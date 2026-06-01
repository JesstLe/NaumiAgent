"""Deterministic fusion-boundary audit helpers."""

from __future__ import annotations

import re

from naumi_agent.tools import analysis_common

AI_CALL_PATTERNS = [
    (r"(?:litellm|openai)\.\w+\(", "LLM API 调用"),
    (r"(?:router|model)\.call\s*\(", "模型路由调用"),
    (r"(?:ChatCompletion|completion|generate|chat)\s*\(", "生成式 AI 接口"),
    (r"(?:embedding|embed)\s*\(", "向量嵌入调用"),
    (r"(?:classify|predict|analyze)\s*\([^)]*model", "ML 推理调用"),
    (r"temperature\s*=\s*[^0]", "非零温度参数 (随机采样开启)"),
    (r"(?:prompt|system_prompt)\s*=", "Prompt 变量定义"),
    (r"\.content\s*$", "LLM 响应内容提取"),
]

PRECISION_CRITICAL_PATTERNS = [
    (r"(?:float|Decimal|Money|Currency)\s*\(", "金融/货币计算"),
    (r"(?:sum|total|balance|amount|price)\s*[+\-*/]?", "金额聚合运算"),
    (r"(?:sort|rank|compare|max|min)\s*\([^)]*\)", "排序/比较/排名"),
    (r"(?:hash|sha|md5|crc|checksum)\s*\(", "哈希/校验和"),
    (r"(?:uuid|uid|guid)\s*\(", "唯一 ID 生成"),
    (r"(?:date|datetime|timestamp)\s*\([^)]*\)", "时间戳计算"),
    (r"(?:index|offset|position|cursor)\s*[+\-*/]?=", "索引/偏移量计算"),
    (r"(?:assert|assertEquals|assertAlmostEqual)\s*\(", "精确断言验证"),
    (r"int\s*\([^)]+\)|float\s*\([^)]+\)", "类型转换 (精度敏感)"),
    (r"\[\s*\d+\s*:\s*\d+\s*\]", "精确切片/分页"),
]

DANGER_FUSION_PATTERNS = [
    (r"(?:response|result|output|content)\.?\w*\s*(?:=|as)\s*int", "AI 输出直接转整数"),
    (r"(?:response|result|output|content)\.?\w*\s*(?:=|as)\s*float", "AI 输出直接转浮点"),
    (r"json\.loads\s*\(\s*(?:result|response|output|content)", "AI 输出直接反序列化"),
    (r"eval\s*\(\s*(?:result|response|output)", "AI 输出直接 eval 执行"),
    (r"(?:sql|cursor|execute)\s*\([^)]*(?:result|response|output)", "AI 输出直接拼接 SQL"),
    (r"(?:subprocess|os\.system)\s*\([^)]*(?:result|response)", "AI 输出直接执行命令"),
    (r"open\s*\([^)]*(?:result|response|output)", "AI 输出直接用于文件路径"),
    (r"(?:url|href|link)\s*[+*=]\s*(?:result|response|output)", "AI 输出直接拼接 URL"),
]

OVERDETERMINED_PATTERNS = [
    (
        r"if\s+.+\s*:\s*\n\s*if\s+.+\s*:\s*\n\s*if\s+.+\s*:",
        "三层以上嵌套 if-else (可能适合 AI 分类)",
    ),
    (
        r"(?:re\.compile|regex|pattern)\s*=\s*[\"'].*\|.*\|.*\|",
        "复杂正则 (4+ 分支，可能适合 AI 匹配)",
    ),
    (
        r"switch|match\s+\w+:\s*\n(\s*case\s+.+\s*\n){5,}",
        "庞大 match/case 分支 (可能适合 AI 路由)",
    ),
    (
        r"(?:format|template|render)\s*\(.*\{.*\{.*\{",
        "复杂模板渲染 (3+ 层嵌套变量，可能适合 AI 生成)",
    ),
]


def scan_fusion(target: str) -> str:
    """Scan deterministic/probabilistic boundaries and unsafe AI handoffs."""
    findings: list[str] = []
    source = analysis_common.read_sources(analysis_common.resolve_target(target))

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")
    total_lines = len(lines)

    findings.append("## 1. 概率区 (Probabilistic Zones — AI/LLM)")
    ai_zones: dict[str, list[int]] = {}
    for pattern, label in AI_CALL_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                ai_zones.setdefault(label, []).append(index)

    total_ai_calls = sum(len(line_nos) for line_nos in ai_zones.values())
    if ai_zones:
        findings.append(
            f"- 检测到 **{total_ai_calls}** 处 AI 调用，"
            f"**{len(ai_zones)}** 类："
        )
        for label, line_nos in sorted(ai_zones.items(), key=lambda item: -len(item[1])):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- 未检测到 AI/LLM 调用 — 纯决定论系统")
    findings.append("")

    findings.append("## 2. 决定论区 (Deterministic Zones — 精度敏感)")
    det_zones: dict[str, list[int]] = {}
    for pattern, label in PRECISION_CRITICAL_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                det_zones.setdefault(label, []).append(index)

    total_det = sum(len(line_nos) for line_nos in det_zones.values())
    if det_zones:
        findings.append(
            f"- 检测到 **{total_det}** 处精度敏感操作，"
            f"**{len(det_zones)}** 类："
        )
        for label, line_nos in sorted(det_zones.items(), key=lambda item: -len(item[1])):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- 未检测到精度敏感操作")
    findings.append("")

    findings.append("## 3. 危险融合点 (Danger Zones — AI 输出→精度敏感)")
    danger_hits: list[tuple[str, int, str]] = []
    for pattern, desc in DANGER_FUSION_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                danger_hits.append((desc, index, line.strip()))

    if danger_hits:
        findings.append(
            f"- ⚠️ 发现 **{len(danger_hits)}** 处危险融合 — "
            f"AI 输出未经验证直接进入精度敏感操作："
        )
        for desc, line_no, line_text in danger_hits[:8]:
            short = line_text[:75] + ("..." if len(line_text) > 75 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
        findings.append("- 🔴 这些点需要插入验证层 (类型检查/边界断言/格式校验)")
    else:
        findings.append("- ✅ AI 输出与精度操作之间有适当的验证层")
    findings.append("")

    findings.append("## 4. 过度决定论区 (Over-Determined — 可引入 AI)")
    over_det: list[tuple[str, int]] = []
    for pattern, desc in OVERDETERMINED_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                over_det.append((desc, index))

    if over_det:
        findings.append(
            f"- 发现 **{len(over_det)}** 处过于复杂的决定论逻辑，"
            f"可能适合用 AI 替代："
        )
        for desc, line_no in over_det:
            findings.append(f"  - L{line_no}: {desc}")
    else:
        findings.append("- 决定论逻辑复杂度适中")
    findings.append("")

    ai_ratio = total_ai_calls / max(total_lines / 50, 1)
    det_ratio = total_det / max(total_lines / 50, 1)
    danger_penalty = min(len(danger_hits) * 0.15, 0.6)
    overdet_bonus = min(len(over_det) * 0.05, 0.15)

    has_both = min(ai_ratio + det_ratio, 0.3) if ai_zones and det_zones else 0.0
    fusion_score = (
        has_both * 0.3
        + min(ai_ratio, 1.0) * 0.15
        + min(det_ratio, 1.0) * 0.15
        - danger_penalty
        + overdet_bonus
        + 0.25
    )
    fusion_score = max(0.0, min(1.0, fusion_score))

    findings.append("## 5. 融合架构评分")
    findings.append(f"- **综合评分: {fusion_score:.0%}**")
    findings.append(
        f"- 概率区密度: {min(ai_ratio, 1.0):.0%} "
        f"({total_ai_calls} 处 AI 调用)"
    )
    findings.append(
        f"- 决定论区密度: {min(det_ratio, 1.0):.0%} "
        f"({total_det} 处精度操作)"
    )
    findings.append(f"- 危险融合扣分: -{danger_penalty:.0%}")
    findings.append(f"- 优化空间加分: +{overdet_bonus:.0%}")

    if danger_hits:
        findings.append(f"- 🔴 首要行动: 在 {len(danger_hits)} 处危险融合点插入验证层")
    elif fusion_score >= 0.7:
        findings.append("- ✅ 概率与决定论边界清晰，融合架构成熟")
    elif fusion_score >= 0.4:
        findings.append("- ⚠️ 融合架构部分建立，需加强边界防护")
    else:
        findings.append(
            "- ❌ 系统偏向单一范式，建议重新审视哪些模块适合 AI、"
            "哪些必须用确定论代码"
        )

    return "\n".join(findings)


def build_fusion_inventory_script(target: str) -> str:
    """Build a dependency-free deterministic/probabilistic boundary scanner."""
    return f'''\
"""Fusion boundary inventory script generated by analysis_fusion."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TARGET = {target!r}
SOURCE_SUFFIXES = {{".py", ".js", ".ts", ".tsx", ".jsx"}}
AI_PATTERNS = [
    (r"(?:litellm|openai|anthropic)\\.\\w+\\(", "LLM API 调用"),
    (r"(?:router|model)\\.call\\s*\\(", "模型路由调用"),
    (r"temperature\\s*=\\s*[^0]", "非零温度"),
    (r"(?:prompt|system_prompt)\\s*=", "Prompt 构造"),
    (r"\\.content\\b", "LLM 内容提取"),
]
DETERMINISTIC_PATTERNS = [
    (r"(?:Decimal|float|int)\\s*\\(", "数值转换"),
    (r"(?:hashlib|sha256|md5|checksum)", "哈希/校验"),
    (r"(?:sort|sorted|rank|compare)", "排序/比较"),
    (r"(?:assert|raise|pydantic|schema|validate)", "验证/断言"),
]
DANGER_PATTERNS = [
    (r"json\\.loads\\s*\\(\\s*(?:result|response|output|content)", "AI 输出直接 JSON 解析"),
    (r"eval\\s*\\(\\s*(?:result|response|output|content)", "AI 输出直接 eval"),
    (r"(?:cursor|execute)\\s*\\([^)]*(?:result|response|output|content)", "AI 输出进入 SQL"),
    (
        r"(?:subprocess|os\\.system)\\s*\\([^)]*(?:result|response|output|content)",
        "AI 输出进入命令",
    ),
    (r"(?:open|Path)\\s*\\([^)]*(?:result|response|output|content)", "AI 输出进入路径"),
    (r"(?:int|float|Decimal)\\s*\\(\\s*(?:result|response|output|content)", "AI 输出直接数值转换"),
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
    ai_hits = find_hits(source, AI_PATTERNS)
    deterministic_hits = find_hits(source, DETERMINISTIC_PATTERNS)
    danger_hits = find_hits(source, DANGER_PATTERNS)
    return {{
        "path": str(path),
        "found": True,
        "ai_hits": ai_hits[:80],
        "deterministic_hits": deterministic_hits[:80],
        "danger_hits": danger_hits[:80],
        "validation_contracts": build_validation_contracts(danger_hits),
    }}


def build_validation_contracts(danger_hits: list[dict[str, object]]) -> list[dict[str, object]]:
    contracts = []
    for hit in danger_hits:
        contracts.append({{
            "line": hit["line"],
            "risk": hit["label"],
            "required_guards": [
                "类型校验",
                "格式/schema 校验",
                "范围或白名单校验",
                "失败时拒绝执行而不是自动修复",
            ],
        }})
    return contracts


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
            "ai_hits": sum(len(item.get("ai_hits", [])) for item in files),
            "deterministic_hits": sum(len(item.get("deterministic_hits", [])) for item in files),
            "danger_hits": sum(len(item.get("danger_hits", [])) for item in files),
        }},
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_fusion_report(target: str, scan_evidence: str) -> str:
    """Build deterministic fusion-boundary audit output."""
    script = build_fusion_inventory_script(target)
    return (
        "## Fusion 确定性边界审计\n"
        "- 执行锚点: 概率区/决定论区静态 inventory + 验证层契约。\n"
        "- 审计目标: AI 输出进入数值、路径、SQL、命令、JSON 等决定论边界的风险。\n\n"
        f"## 静态融合扫描\n{scan_evidence}\n\n"
        "## Fusion Inventory Script\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python fusion_inventory.py <source_file_or_directory>`。\n"
        "- 输出 `ai_hits`、`deterministic_hits`、`danger_hits` 和验证层契约。\n"
        "- 脚本只读源码，不调用模型、不执行目标代码。\n\n"
        "## 验证层契约\n"
        "- AI 输出进入决定论代码前必须经过类型、schema、范围、白名单校验。\n"
        "- SQL/命令/路径边界必须拒绝自由文本直通，改用枚举、参数化或安全 builder。\n"
        "- 数值边界必须声明单位、范围、精度和失败行为。\n\n"
        "## 融合改造计划\n"
        "1. 先修复所有 `danger_hits`，让概率输出不能直接触发副作用。\n"
        "2. 为每个验证层补拒绝路径测试和异常输入测试。\n"
        "3. 将 inventory JSON 固化为架构边界回归样本。\n"
    )
