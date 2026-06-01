"""Deterministic trace-verification helpers for ZKP analysis."""

from __future__ import annotations

import re

from naumi_agent.tools import analysis_common

UNVERIFIED_OUTPUT_PATTERNS = [
    (r"return\s+(?:result|response|output|content|summary)", "直接返回 AI 输出 (无引用轨迹)"),
    (
        r"(?:result|answer|summary)\s*=\s*(?:await\s+)?(?:llm|model|router)",
        "AI 输出赋值无验证层",
    ),
    (
        r"(?:print|display|show|render)\s*\(\s*(?:result|response)",
        "AI 输出直接展示 (无来源标注)",
    ),
    (r"json\.loads\s*\(\s*(?:result|response)", "AI 输出反序列化 (无结构验证)"),
    (r"(?:summary|conclusion)\s*=\s*[^#\n]{0,50}$", "摘要赋值 (无引用来源)"),
]

CITATION_PATTERNS = [
    (r"(?:source|reference|citation|cite)\s*[:=]", "引用/来源标注"),
    (r"(?:line_no|lineno|location|offset)\s*[:=]", "行号/位置定位"),
    (r"(?:chunk|document|file|page)_?id\s*[:=]", "文档/块 ID 引用"),
    (r"\\?\[(\d+)\\?\]", "数字引用标记 [N]"),
    (r"(?:provenance|origin|trace)\s*[:=]", "来源追溯字段"),
    (r"(?:confidence|certainty|score)\s*[:=]", "置信度评分"),
]

CLAIM_GAP_PATTERNS = [
    (r"(?:因此|所以|综上|可以看出|说明|证明)\s*", "无支撑的推理结论词"),
    (r"(?:obviously|clearly|it is known|obviously)\s*", "无支撑的英文断言词"),
    (r"(?:据统计|数据显示|研究表明)\s*(?!.*(?:来源|引用|http|ref))", "无引用的数据声称"),
    (r"\d+(?:\.\d+)?%", "百分比数据 (需来源验证)"),
    (r"(?:the\s+)?(?:result|output|answer)\s+is\s+", "直接陈述结论 (无推导过程)"),
]

VALIDATION_PATTERNS = [
    (r"(?:verify|validate|cross.?check|corroborate)\s*\(", "交叉验证逻辑"),
    (r"(?:spot.?check|sample|audit)\s*\(", "抽检验证"),
    (r"(?:hash|checksum|digest)\s*[=<>]", "哈希校验"),
    (r"(?:diff|compare|match)\s*\([^)]*(?:expected|baseline|golden)", "与基准比对"),
    (r"(?:ground.?truth|reference|canonical)\s*", "真值/基准数据引用"),
]


def scan_zkp(target: str) -> str:
    """Scan AI output traceability, citation infrastructure, and validators."""
    findings: list[str] = []
    source = analysis_common.read_sources(analysis_common.resolve_target(target))

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    findings.append("## 1. 未验证输出检测 (Unverified AI Outputs)")
    unverified: list[tuple[str, int, str]] = []
    for pattern, desc in UNVERIFIED_OUTPUT_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                unverified.append((desc, index, line.strip()))

    if unverified:
        findings.append(f"- ⚠️ 发现 **{len(unverified)}** 处 AI 输出未经轨迹校验：")
        for desc, line_no, line_text in unverified[:8]:
            short = line_text[:70] + ("..." if len(line_text) > 70 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
    else:
        findings.append("- ✅ AI 输出均经过验证层")
    findings.append("")

    findings.append("## 2. 引用基础设施 (Citation Infrastructure)")
    citation_hits: dict[str, list[int]] = {}
    for pattern, label in CITATION_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                citation_hits.setdefault(label, []).append(index)

    total_citations = sum(len(line_nos) for line_nos in citation_hits.values())
    if citation_hits:
        findings.append(
            f"- 检测到 **{total_citations}** 处引用机制，"
            f"**{len(citation_hits)}** 类："
        )
        for label, line_nos in sorted(
            citation_hits.items(),
            key=lambda item: -len(item[1]),
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无任何引用机制 — AI 输出无法溯源")
    findings.append("")

    findings.append("## 3. 事实-证据缺口 (Claim-Fact Gaps)")
    claim_gaps: list[tuple[str, int, str]] = []
    for pattern, desc in CLAIM_GAP_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                claim_gaps.append((desc, index, line.strip()))

    if claim_gaps:
        findings.append(f"- ⚠️ 发现 **{len(claim_gaps)}** 处可能是无支撑的事实声称：")
        for desc, line_no, line_text in claim_gaps[:8]:
            short = line_text[:70] + ("..." if len(line_text) > 70 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
        findings.append("- 🔴 这些声称需要引用轨迹来证明其真实性")
    else:
        findings.append("- ✅ 事实声称均有引用支撑")
    findings.append("")

    findings.append("## 4. 验证层 (Validation Layer)")
    validation_hits: dict[str, list[int]] = {}
    for pattern, label in VALIDATION_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                validation_hits.setdefault(label, []).append(index)

    if validation_hits:
        total_val = sum(len(line_nos) for line_nos in validation_hits.values())
        findings.append(
            f"- 检测到 **{total_val}** 处验证机制，"
            f"**{len(validation_hits)}** 类："
        )
        for label, line_nos in validation_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无验证层 — 无法确认 AI 输出的真实性")
    findings.append("")

    citation_score = min(len(citation_hits) / 4.0, 1.0)
    validation_score = min(len(validation_hits) / 3.0, 1.0)
    unverified_penalty = min(len(unverified) * 0.1, 0.4)
    claim_penalty = min(len(claim_gaps) * 0.05, 0.3)

    zkp_score = (
        citation_score * 0.35
        + validation_score * 0.35
        - unverified_penalty
        - claim_penalty
        + 0.3
    )
    zkp_score = max(0.0, min(1.0, zkp_score))

    findings.append("## 5. 可验证计算评分 (Verifiability Score)")
    findings.append(f"- **综合评分: {zkp_score:.0%}**")
    findings.append(f"- 引用基础设施: {citation_score:.0%}")
    findings.append(f"- 验证层完备度: {validation_score:.0%}")
    findings.append(f"- 未验证输出扣分: -{unverified_penalty:.0%}")
    findings.append(f"- 事实缺口扣分: -{claim_penalty:.0%}")

    if zkp_score >= 0.7:
        findings.append("- ✅ 具备较强的可验证性，AI 输出可溯源可校验")
    elif zkp_score >= 0.4:
        findings.append("- ⚠️ 部分具备可验证性，需加强引用和验证层")
    else:
        findings.append("- ❌ AI 输出几乎不可验证 — 建议引入引用轨迹树和交叉校验机制")

    return "\n".join(findings)


def build_zkp_trace_script(target: str) -> str:
    """Build a dependency-free citation/trace verifier."""
    return f'''\
"""Citation trace verifier generated by analysis_zkp."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TARGET = {target!r}
SOURCE_SUFFIXES = {{".py", ".md", ".txt", ".json", ".yaml", ".yml"}}
UNVERIFIED_PATTERNS = [
    (r"return\\s+(?:result|response|output|content|summary)", "直接返回 AI 输出"),
    (r"(?:result|answer|summary)\\s*=\\s*(?:await\\s+)?(?:llm|model|router)", "AI 输出赋值"),
    (r"json\\.loads\\s*\\(\\s*(?:result|response|output|content)", "AI 输出反序列化"),
]
CITATION_PATTERNS = [
    (r"(?:source|reference|citation|cite)\\s*[:=]", "来源字段"),
    (r"(?:line_no|lineno|location|offset)\\s*[:=]", "位置字段"),
    (r"(?:provenance|origin|trace)\\s*[:=]", "轨迹字段"),
    (r"(?:confidence|certainty|score)\\s*[:=]", "置信度字段"),
]
VALIDATION_PATTERNS = [
    (r"(?:verify|validate|cross.?check|corroborate)\\s*\\(", "交叉验证"),
    (r"(?:hash|checksum|digest)", "哈希校验"),
    (r"(?:ground.?truth|reference|canonical)", "真值/基准"),
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
    unverified = find_hits(source, UNVERIFIED_PATTERNS)
    citations = find_hits(source, CITATION_PATTERNS)
    validations = find_hits(source, VALIDATION_PATTERNS)
    return {{
        "path": str(path),
        "found": True,
        "unverified_outputs": unverified[:80],
        "citations": citations[:80],
        "validations": validations[:80],
        "trace_contract": build_trace_contract(unverified, citations, validations),
    }}


def build_trace_contract(
    unverified: list[dict[str, object]],
    citations: list[dict[str, object]],
    validations: list[dict[str, object]],
) -> dict[str, object]:
    return {{
        "requires_trace_tree": bool(unverified),
        "citation_fields_present": len(citations),
        "validation_fields_present": len(validations),
        "required_fields": [
            "claim_id",
            "source_path",
            "line_start",
            "line_end",
            "quoted_evidence",
            "inference_step",
            "confidence",
            "verifier_status",
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
            "unverified_outputs": sum(len(item.get("unverified_outputs", [])) for item in files),
            "citations": sum(len(item.get("citations", [])) for item in files),
            "validations": sum(len(item.get("validations", [])) for item in files),
        }},
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_zkp_report(target: str, scan_evidence: str) -> str:
    """Build deterministic ZKP trace-verification output."""
    script = build_zkp_trace_script(target)
    return (
        "## ZKP 确定性轨迹校验方案\n"
        "- 执行锚点: 不可验证输出 inventory + 引用/验证字段检测 + trace contract。\n"
        "- 审计目标: 每个 AI 结论都能追溯到来源、位置、证据和校验状态。\n\n"
        f"## 静态可验证性扫描\n{scan_evidence}\n\n"
        "## Trace Verifier Script\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python zkp_trace.py <source_file_or_directory>`。\n"
        "- 输出 `unverified_outputs`、`citations`、`validations` 和 `trace_contract`。\n"
        "- 脚本只读源码和文本，不调用模型、不执行目标代码。\n\n"
        "## Trace Contract\n"
        "- 每个 claim 必须包含 source_path、line_start、line_end、quoted_evidence。\n"
        "- 每个推理步骤必须有 inference_step、confidence、verifier_status。\n"
        "- 引用不存在、证据不匹配、置信度缺失时必须拒绝输出高风险结论。\n\n"
        "## 实施计划\n"
        "1. 先把 `unverified_outputs` 包装成 claim 对象。\n"
        "2. 为 claim 增加引用定位和原文片段校验。\n"
        "3. 为数值/事实类 claim 增加 deterministic verifier。\n"
        "4. 将 trace JSON 固化为可验证计算回归样本。\n"
    )
