"""Deterministic DSPy-style prompt maturity helpers."""

from __future__ import annotations

import re
from pathlib import Path

PROMPT_SIGNATURES = [
    r'""".*?(?:You are|你是一个?|##\s|Instructions?|任务|Role).+?"""',
    r'f".*?(?:system|user|assistant).*?"',
    r'(?:PROMPT|prompt|SYSTEM|system_msg|template)\s*[:=]',
]

FEW_SHOT_PATTERNS = [
    r'(?:example|few.?shot|demonstration)\s*[:=]',
    r'#\s*(?:Example|示例|样例)\s*\d',
    r'(?:Input|输入|Question)\s*[:：].*?\n.*?(?:Output|输出|Answer)\s*[:：]',
]

METRIC_PATTERNS = [
    r'(?:metric|score|evaluate|评估|评分|accuracy|f1|precision|recall)\s*[:=(]',
    r'def\s+(?:evaluate|score|metric|assess|judge)',
    r'(?:assert|check|verify)\s+.*?(?:output|result|response)',
]


def scan_dspy(
    files: list[Path],
    source_text: str,
    prompt_target: str,
) -> str:
    """Scan source for prompt templates, few-shot examples, and metrics."""
    findings: list[str] = []

    prompt_locs: list[str] = []
    for pattern in PROMPT_SIGNATURES:
        matches = list(re.finditer(pattern, source_text, re.DOTALL | re.IGNORECASE))
        for match in matches:
            start = max(0, match.start() - 30)
            ctx = source_text[start : match.end()].replace("\n", " ")[:80]
            prompt_locs.append(ctx)

    findings.append(f"- 发现 Prompt 模板: {len(prompt_locs)} 处")
    for loc in prompt_locs[:8]:
        truncated = loc if len(loc) <= 78 else loc[:75] + "..."
        findings.append(f"  - `{truncated}`")

    few_shot_count = 0
    few_shot_locs: list[str] = []
    for pattern in FEW_SHOT_PATTERNS:
        matches = list(re.finditer(pattern, source_text, re.IGNORECASE))
        few_shot_count += len(matches)
        for match in matches[:4]:
            line_start = source_text.rfind("\n", 0, match.start()) + 1
            line_end = source_text.find("\n", match.end())
            line = source_text[line_start:line_end].strip()[:80]
            few_shot_locs.append(line)
    if few_shot_count:
        findings.append(f"- Few-shot 示例: {few_shot_count} 处")
        for loc in few_shot_locs[:5]:
            findings.append(f"  - `{loc}`")
    else:
        findings.append("- ⚠️ 未发现 Few-shot 示例（强烈建议添加）")

    metric_count = 0
    metric_locs: list[str] = []
    for pattern in METRIC_PATTERNS:
        matches = list(re.finditer(pattern, source_text, re.IGNORECASE))
        metric_count += len(matches)
        for match in matches[:4]:
            line_start = source_text.rfind("\n", 0, match.start()) + 1
            line_end = source_text.find("\n", match.end())
            line = source_text[line_start:line_end].strip()[:80]
            metric_locs.append(line)
    if metric_count:
        findings.append(f"- 评估函数/Metric: {metric_count} 处")
        for loc in metric_locs[:5]:
            findings.append(f"  - `{loc}`")
    else:
        findings.append("- ⚠️ 未发现评估函数/Metric（这是 DSPy 的核心！）")

    prompt_lengths: list[int] = []
    for match in re.finditer(r'""".+?"""', source_text, re.DOTALL):
        prompt_lengths.append(match.end() - match.start())
    for match in re.finditer(r"'[^']{50,}'", source_text):
        prompt_lengths.append(match.end() - match.start())
    if prompt_lengths:
        avg_len = sum(prompt_lengths) // len(prompt_lengths)
        max_len = max(prompt_lengths)
        findings.append(
            f"- Prompt 长度分布: 平均 {avg_len} 字符, "
            f"最长 {max_len} 字符 ({len(prompt_lengths)} 个)"
        )

    hardcoded = len(
        re.findall(
            r'(?:SYSTEM_PROMPT|system_prompt)\s*=\s*(?:f?["\'])',
            source_text,
        )
    )
    configurable = len(
        re.findall(
            r'(?:prompt|template|system_msg)\s*=\s*(?:config|settings|load|read|yaml)',
            source_text,
        )
    )
    findings.append(f"- Prompt 管理: {hardcoded} 个硬编码, {configurable} 个可配置")

    score = 0
    if prompt_locs:
        score += 20
    if few_shot_count > 0:
        score += 25
    if metric_count > 0:
        score += 30
    if configurable > 0:
        score += 15
    if prompt_lengths:
        score += 10
    findings.append(
        f"\n- DSPy 工程成熟度: {score}/100 "
        f"({'优秀' if score >= 80 else '及格' if score >= 50 else '需要改进'})"
    )
    if prompt_target:
        findings.append(f"- 优化目标: {prompt_target}")

    return "\n".join(findings)


def build_dspy_baseline_metric(prompt_target: str = "") -> str:
    """Build an executable baseline metric function for prompt optimization."""
    target = prompt_target.strip() or "目标任务"
    return f'''\
def score_output(input_text: str, output_text: str) -> dict[str, float | bool | str]:
    """Baseline metric for DSPy-style prompt optimization: {target}."""
    normalized = output_text.strip()
    has_content = bool(normalized)
    is_not_error = "error" not in normalized.lower() and "traceback" not in normalized.lower()
    has_structure = any(marker in normalized for marker in ("1.", "- ", "##", "：", ":"))
    length_ok = 20 <= len(normalized) <= 4000
    score = sum([has_content, is_not_error, has_structure, length_ok]) / 4
    return {{
        "score": score,
        "has_content": has_content,
        "is_not_error": is_not_error,
        "has_structure": has_structure,
        "length_ok": length_ok,
        "reason": "baseline heuristic; replace with task-specific ground truth checks",
    }}
'''


def format_dspy_report(
    scan_evidence: str,
    prompt_target: str,
    files: list[Path],
) -> str:
    """Format deterministic DSPy maturity evidence for tool output."""
    metric_code = build_dspy_baseline_metric(prompt_target)
    return (
        "## DSPy 静态成熟度扫描\n"
        f"- 扫描文件数：{len(files)}\n\n"
        f"{scan_evidence}\n\n"
        "## Baseline Metric\n"
        "```python\n"
        f"{metric_code}"
        "```"
    )
