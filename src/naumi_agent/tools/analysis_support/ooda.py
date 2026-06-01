"""Deterministic OODA loop resilience analysis helpers."""

from __future__ import annotations

import re
from pathlib import Path

FRAGILE_PATTERNS = [
    (
        r"find_element\s*\(\s*By\.(?:XPATH|CSS_SELECTOR)",
        "Selenium 硬编码选择器",
    ),
    (r"\.select\s*\(\s*[\"'][^\"']*[\"']\s*\)", "CSS 硬编码选择器"),
    (r"driver\.find_element", "WebDriver 硬编码定位"),
    (
        r"(?:url|endpoint|host)\s*=\s*[\"']https?://[^\"']+[\"']",
        "硬编码 URL",
    ),
    (r"sleep\s*\(\s*\d+\s*\)", "硬编码等待时间"),
]

ERROR_HANDLING_PATTERNS = [
    (r"try\s*:", "try 块"),
    (r"except\s+\w+", "具体异常捕获"),
    (r"finally\s*:", "finally 清理"),
    (r"raise\s+\w+", "主动抛出异常"),
]

OODA_STAGE_PATTERNS = [
    r"(?:observe|monitor|detect)",
    r"(?:orient|analyze|judge)",
    r"(?:decide|choose|plan)",
    r"(?:act|execute|perform)",
]


def scan_ooda(files: list[Path], source_text: str, task: str) -> str:
    """Scan code for fragile automation paths and missing OODA stages."""
    del files

    findings: list[str] = []
    fragile_count = 0
    for pattern, label in FRAGILE_PATTERNS:
        count = len(re.findall(pattern, source_text, re.IGNORECASE))
        if count:
            fragile_count += count
            findings.append(f"  - {label}: {count} 处")
    if fragile_count:
        findings.insert(0, f"- 脆弱模式: {fragile_count} 处")

    error_count = 0
    for pattern, _label in ERROR_HANDLING_PATTERNS:
        error_count += len(re.findall(pattern, source_text, re.IGNORECASE))
    findings.append(
        f"- 错误处理: {error_count} 处"
        if error_count
        else "- 错误处理: 无（极易崩溃）"
    )

    ooda_stages = sum(
        bool(re.findall(pattern, source_text, re.IGNORECASE))
        for pattern in OODA_STAGE_PATTERNS
    )
    findings.append(f"- OODA 覆盖: {ooda_stages}/4")

    fragility = fragile_count * 10 + (0 if error_count else 30) + (
        4 - ooda_stages
    ) * 10
    level = (
        "CRITICAL"
        if fragility > 80
        else "HIGH"
        if fragility > 50
        else "MEDIUM"
        if fragility > 25
        else "LOW"
    )
    findings.append(f"- 脆弱性评分: {fragility} ({level})")
    if task:
        findings.append(f"- 任务: {task[:200]}")
    return "\n".join(findings)


def build_ooda_report(scan_evidence: str, files: list[Path], task: str = "") -> str:
    """Build the deterministic mission-command report for OODA hardening."""
    score = extract_ooda_resilience_score(scan_evidence)
    return "\n".join(
        [
            "## OODA 确定性任务指挥",
            f"- Commander's Intent: {task or '在保持可恢复性的前提下完成目标任务。'}",
            f"- 扫描文件数：{len(files)}",
            "",
            "## 脆弱性扫描",
            scan_evidence,
            "",
            "## OODA Loop Design",
            "### Observe",
            "- 采集运行状态、错误类型、外部依赖响应、选择器/API 命中率。",
            "- Failure mode: 无观测会让失败退化成静默卡死。",
            "- Recovery: 每个外部动作必须返回结构化状态和错误原因。",
            "### Orient",
            "- 将错误归类为输入错误、外部依赖失败、选择器漂移、权限/预算限制。",
            "- Failure mode: 错误分类不清会触发错误修复路径。",
            "- Recovery: 使用规则优先分类；无法分类时进入人工/LLM 深化分析。",
            "### Decide",
            "- 基于风险等级选择 retry、fallback、降级、隔离或停止。",
            "- Failure mode: 固定单路径会在环境变化时反复失败。",
            "- Recovery: 每个决策必须有最大重试次数和 backoff 策略。",
            "### Act",
            "- 执行最小动作并记录结果；动作后立即回到 Observe。",
            "- Failure mode: 批量动作会扩大爆炸半径。",
            "- Recovery: 使用小步提交和 targeted validation。",
            "",
            "## Self-Healing Mechanisms",
            "- Failure detection: 错误类型、超时、空结果、重复失败计数。",
            "- Auto-retry: 只对瞬时依赖失败重试，禁止对逻辑错误无限重试。",
            "- Fallback: 选择备用 selector/API/cache/只读诊断路径。",
            "- Isolation: 高风险动作单独隔离，失败不污染全局状态。",
            "",
            "## Anti-Fragility Checklist",
            "- 无硬编码 URL/selector/wait；必须有配置或发现机制。",
            "- 无 silent failure；用户可见错误必须包含下一步动作。",
            "- 无单一路径；关键动作至少有 fallback 或 stop condition。",
            "- 每个修复必须绑定回归测试或真实场景验证。",
            "",
            f"## Resilience Score\n- {score}/10",
        ]
    )


def extract_ooda_resilience_score(scan_evidence: str) -> int:
    """Convert the deterministic fragility score into a bounded resilience score."""
    match = re.search(r"脆弱性评分:\s*(\d+)\s*\((\w+)\)", scan_evidence)
    if not match:
        return 5
    fragility = int(match.group(1))
    return max(1, min(10, 10 - fragility // 12))
