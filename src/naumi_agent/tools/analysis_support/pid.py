"""Deterministic PID closed-loop audit helpers."""

from __future__ import annotations

import re

from naumi_agent.tools import analysis_common

OPEN_LOOP_PATTERNS = [
    (
        r"(?:step_1|step_2|step_3|first|second|third)\s*[:=]",
        "步骤式线性执行 (无反馈检查点)",
    ),
    (
        r"(?:then|after that|next)\s+(?:run|execute|call|do)",
        "链式顺序调用 (无中间验证)",
    ),
    (r"(?:pipeline|chain|workflow)\s*=\s*\[", "线性流水线定义 (无分支纠偏)"),
    (
        r"(?:await\s+\w+\([^)]*\)\s*;?\s*\n\s*await\s+\w+){3,}",
        "连续 await 无验证 (盲目串联)",
    ),
    (
        r"(?:for|while)\s+[^:]+:\s*\n(\s*\w+\.\w+\([^)]*\)\s*\n){5,}",
        "循环内批量执行无退出条件",
    ),
]

FEEDBACK_PATTERNS = [
    (r"(?:assert|verify|check|validate)\s*\(", "断言/验证检查点"),
    (r"(?:monitor|observe|measure|sense)\s*\(", "监控/观测点"),
    (r"(?:status|state|progress)\s*[=!<>]", "状态比较检查"),
    (r"(?:retry|fallback|recovery|rollback)\s*\(", "重试/回滚机制"),
    (r"(?:error_rate|success_rate|threshold)\s*[=!<>]", "阈值监控"),
    (r"(?:if|while)\s+[^:]*(?:result|status|response)", "结果条件分支"),
    (r"(?:log|metric|telemetry)\s*[.(]", "日志/指标采集"),
]

ERROR_ACCUMULATION_PATTERNS = [
    (r"total\s*[+\-*/]?=\s*\w+", "累加器 (误差可能累积)"),
    (r"\w+\s*[+\-*/]?=\s*\w+\s*[+\-*/]\s*\w+", "链式运算 (精度可能漂移)"),
    (r"(?:batch|chunk|buffer)\s*\[", "批量处理 (单条失败影响全局)"),
    (r"(?:append|extend|accumulate)\s*\([^)]*\)", "数据累积 (可能无限增长)"),
    (r"while\s+True\s*:", "无限循环 (无退出保证)"),
]

PREDICTIVE_PATTERNS = [
    (r"(?:timeout|deadline|time_limit|expiry)\s*[=<>]", "超时/截止时间检测"),
    (r"(?:rate_limit|throttle|backpressure)\s*", "速率限制/背压机制"),
    (r"(?:memory_usage|heap_size|rss)\s*[=<>]", "内存使用监控"),
    (r"(?:trend|slope|derivative|velocity)\s*[=<>]", "趋势/变化率分析"),
    (r"(?:predict|forecast|anticipate|estimate)\s*\(", "预测性操作"),
]


def scan_pid(target: str) -> str:
    """Scan closed-loop control readiness from source code evidence."""
    findings: list[str] = []
    source = analysis_common.read_sources(analysis_common.resolve_target(target))

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    findings.append("## 1. 开环检测 (Open-Loop Pipelines)")
    open_hits: list[tuple[str, int]] = []
    for pattern, desc in OPEN_LOOP_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                open_hits.append((desc, index))

    if open_hits:
        findings.append(f"- ⚠️ 发现 **{len(open_hits)}** 处开环执行模式 — 线性推进无反馈纠偏：")
        for desc, line_no in open_hits[:8]:
            findings.append(f"  - L{line_no}: {desc}")
    else:
        findings.append("- ✅ 未检测到明显的开环执行模式")
    findings.append("")

    findings.append("## 2. 反馈检查点 (Feedback Checkpoints)")
    feedback_zones: dict[str, list[int]] = {}
    for pattern, label in FEEDBACK_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                feedback_zones.setdefault(label, []).append(index)

    total_checkpoints = sum(len(line_nos) for line_nos in feedback_zones.values())
    if feedback_zones:
        findings.append(
            f"- 检测到 **{total_checkpoints}** 个反馈检查点，"
            f"**{len(feedback_zones)}** 类："
        )
        for label, line_nos in sorted(
            feedback_zones.items(),
            key=lambda item: -len(item[1]),
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 完全没有反馈检查点 — 典型的开环系统")
    findings.append("")

    findings.append("## 3. 误差累积风险 (Error Accumulation)")
    accum_hits: list[tuple[str, int, str]] = []
    for pattern, desc in ERROR_ACCUMULATION_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                accum_hits.append((desc, index, line.strip()))

    if accum_hits:
        findings.append(f"- ⚠️ 发现 **{len(accum_hits)}** 处误差累积风险：")
        for desc, line_no, line_text in accum_hits[:6]:
            short = line_text[:70] + ("..." if len(line_text) > 70 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
        findings.append("- 💡 建议: 引入 I (积分) 环节 — 定期清零累积器，记录历史误差趋势")
    else:
        findings.append("- ✅ 误差累积风险较低")
    findings.append("")

    findings.append("## 4. 预测性纠偏能力 (Derivative / D Term)")
    pred_zones: dict[str, list[int]] = {}
    for pattern, label in PREDICTIVE_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                pred_zones.setdefault(label, []).append(index)

    if pred_zones:
        findings.append(f"- 检测到 **{len(pred_zones)}** 类预测性机制：")
        for label, line_nos in pred_zones.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无预测性纠偏 — 系统只能事后反应，不能事前预防")
    findings.append("")

    p_score = min(len(feedback_zones) / 4.0, 1.0)
    i_score = 1.0 - min(len(accum_hits) / 5.0, 1.0)
    d_score = min(len(pred_zones) / 3.0, 1.0)
    open_penalty = min(len(open_hits) * 0.1, 0.3)

    pid_score = p_score * 0.40 + i_score * 0.25 + d_score * 0.25 - open_penalty + 0.10
    pid_score = max(0.0, min(1.0, pid_score))

    findings.append("## 5. PID 闭环成熟度评分")
    findings.append(f"- **综合评分: {pid_score:.0%}**")
    findings.append(f"- P (比例/实时纠偏): {p_score:.0%} — {total_checkpoints} 个检查点")
    findings.append(f"- I (积分/历史累积): {i_score:.0%} — {len(accum_hits)} 处累积风险")
    findings.append(f"- D (微分/趋势预测): {d_score:.0%} — {len(pred_zones)} 类预测机制")

    if pid_score >= 0.7:
        findings.append("- ✅ 闭环控制较为成熟，具备动态纠偏能力")
    elif pid_score >= 0.4:
        findings.append("- ⚠️ 具备部分反馈机制，但尚未形成完整闭环")
    else:
        findings.append("- ❌ 系统处于开环状态，建议引入 P→I→D 渐进式改造")

    return "\n".join(findings)


def build_pid_inventory_script(target: str) -> str:
    """Build a dependency-free closed-loop readiness scanner."""
    return f'''\
"""PID closed-loop inventory script generated by analysis_pid."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TARGET = {target!r}
SOURCE_SUFFIXES = {{".py", ".js", ".ts", ".tsx", ".jsx"}}
OPEN_LOOP_PATTERNS = [
    (r"(?:pipeline|chain|workflow)\\s*=\\s*\\[", "线性流水线"),
    (r"(?:step_1|step_2|step_3|first|second|third)\\s*[:=]", "步骤式执行"),
    (r"while\\s+True\\s*:", "无限循环"),
]
FEEDBACK_PATTERNS = [
    (r"(?:assert|verify|check|validate)\\s*\\(", "验证检查点"),
    (r"(?:status|state|progress)\\s*[=!<>]", "状态比较"),
    (r"(?:retry|fallback|recovery|rollback)\\s*\\(", "恢复机制"),
    (r"(?:log|metric|telemetry)\\s*[.(]", "指标/日志"),
]
ACCUMULATION_PATTERNS = [
    (r"total\\s*[+\\-*/]?=\\s*\\w+", "累加器"),
    (r"(?:append|extend|accumulate)\\s*\\([^)]*\\)", "数据累积"),
    (r"(?:batch|chunk|buffer)\\s*\\[", "批量缓冲"),
]
PREDICTIVE_PATTERNS = [
    (r"(?:timeout|deadline|time_limit|expiry)\\s*[=<>]", "超时/截止时间"),
    (r"(?:rate_limit|throttle|backpressure)", "速率限制/背压"),
    (r"(?:trend|slope|derivative|velocity)\\s*[=<>]", "趋势/变化率"),
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


def inspect_file(path: Path) -> dict[str, object]:
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {{"path": str(path), "found": False, "error": str(exc)}}
    open_loop = find_hits(source, OPEN_LOOP_PATTERNS)
    feedback = find_hits(source, FEEDBACK_PATTERNS)
    accumulation = find_hits(source, ACCUMULATION_PATTERNS)
    predictive = find_hits(source, PREDICTIVE_PATTERNS)
    return {{
        "path": str(path),
        "found": True,
        "open_loop": open_loop[:80],
        "feedback": feedback[:80],
        "accumulation": accumulation[:80],
        "predictive": predictive[:80],
        "pid_contract": build_pid_contract(open_loop, feedback, accumulation, predictive),
    }}


def build_pid_contract(
    open_loop: list[dict[str, object]],
    feedback: list[dict[str, object]],
    accumulation: list[dict[str, object]],
    predictive: list[dict[str, object]],
) -> dict[str, object]:
    return {{
        "p_required": len(open_loop) > 0 and len(feedback) == 0,
        "i_required": len(accumulation) > 0,
        "d_required": len(predictive) == 0,
        "minimum_gates": [
            "每个线性步骤后有状态校验",
            "累积器有上限、清零或窗口策略",
            "长循环有 timeout/deadline/backpressure",
            "失败时有 retry/fallback/rollback 或熔断",
        ],
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
            "open_loop": sum(len(item.get("open_loop", [])) for item in files),
            "feedback": sum(len(item.get("feedback", [])) for item in files),
            "accumulation": sum(len(item.get("accumulation", [])) for item in files),
            "predictive": sum(len(item.get("predictive", [])) for item in files),
        }},
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_pid_report(target: str, scan_evidence: str) -> str:
    """Build deterministic PID closed-loop audit output."""
    script = build_pid_inventory_script(target)
    return (
        "## PID 确定性闭环审计\n"
        "- 执行锚点: 开环/反馈/累积/预测 inventory + PID 改造契约。\n"
        "- 审计目标: 把线性执行改造成 P 实时校验、I 历史约束、D 趋势预防。\n\n"
        f"## 静态 PID 扫描\n{scan_evidence}\n\n"
        "## PID Inventory Script\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python pid_inventory.py <source_file_or_directory>`。\n"
        "- 输出 `open_loop`、`feedback`、`accumulation`、`predictive` 和 `pid_contract`。\n"
        "- 脚本只读源码，不执行目标代码。\n\n"
        "## PID 改造契约\n"
        "- P: 每个步骤后必须有状态校验和偏差分支。\n"
        "- I: 累积器必须有窗口、上限、清零或历史错误权重策略。\n"
        "- D: 长循环、批处理、外部调用必须有 timeout、deadline、背压或趋势预测。\n"
        "- Actuator: 失败时必须明确继续、重试、回滚、熔断或人工接管。\n\n"
        "## 渐进式实施计划\n"
        "1. 先补 P 检查点，避免盲目串联执行。\n"
        "2. 再补 I 历史约束，避免误差和资源无限累积。\n"
        "3. 最后补 D 预测机制，提前处理超时、内存、速率和趋势风险。\n"
    )
