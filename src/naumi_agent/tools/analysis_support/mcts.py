"""Deterministic MCTS decision analysis helpers."""

from __future__ import annotations

import math
import re
from pathlib import Path


def scan_mcts(files: list[Path], source_text: str, problem: str) -> str:
    """Analyze decision-space complexity and verification constraints."""
    del files

    findings: list[str] = []

    if_count = len(re.findall(r"\bif\s+", source_text))
    elif_count = len(re.findall(r"\belif\s+", source_text))
    match_cases = len(re.findall(r"\bcase\s+", source_text))
    ternary = len(re.findall(r"\bif\s+.+\s+else\s+", source_text))
    total_branches = if_count + elif_count + match_cases + ternary
    findings.append(
        f"- 决策分支点: {total_branches} 个 "
        f"(if={if_count}, elif={elif_count}, "
        f"match={match_cases}, ternary={ternary})"
    )

    if total_branches > 0:
        est_paths = min(2**total_branches, 10**15)
        if est_paths >= 10**9:
            space_str = f"~10^{int(math.log10(est_paths))} 条路径"
        else:
            space_str = f"{est_paths:,} 条路径"
        findings.append(f"- 估算搜索空间: {space_str} （需要剪枝策略而非穷举）")

    assertions = len(re.findall(r"\bassert\s+", source_text))
    validations = len(
        re.findall(r"(?:validate|check|verify|ensure|guard)\s*\(", source_text)
    )
    type_checks = len(re.findall(r"isinstance\s*\(", source_text))
    findings.append(
        f"- 约束条件: {assertions} assertions, "
        f"{validations} validations, "
        f"{type_checks} type checks"
    )

    raises = re.findall(r"raise\s+(\w+)", source_text)
    unique_raises = set(raises)
    if unique_raises:
        findings.append(f"- 异常路径: {len(raises)} 个 raise ({len(unique_raises)} 种类型)")

    test_functions = re.findall(r"\bdef\s+(test_\w+)", source_text)
    if test_functions:
        findings.append(f"- 已有验证机制: {len(test_functions)} 个测试函数")
    else:
        findings.append("- ⚠️ 无测试覆盖（建议添加回归测试）")

    async_funcs = len(re.findall(r"\basync\s+def\s+", source_text))
    locks = len(re.findall(r"(?:Lock|Semaphore|Event|Condition|Barrier)\s*\(", source_text))
    concurrency_score = async_funcs * 2 + locks * 5
    if concurrency_score > 0:
        findings.append(
            f"- 并发复杂度: {concurrency_score} 分 "
            f"(async={async_funcs}, locks={locks})"
        )

    unique_imports: set[str] = set()
    imports = re.findall(r"^import\s+(\S+)|^from\s+(\S+)", source_text, re.MULTILINE)
    flat_imports = [imp for pair in imports for imp in pair if imp]
    if flat_imports:
        unique_imports = set(flat_imports)
        findings.append(f"- 外部依赖: {len(unique_imports)} 个 (每个依赖都是潜在风险)")

    complexity_score = (
        total_branches * 2
        + len(unique_raises) * 3
        + concurrency_score
        + len(unique_imports)
        - len(test_functions) * 5
    )
    complexity_score = max(0, complexity_score)
    level = (
        "CRITICAL"
        if complexity_score > 100
        else "HIGH"
        if complexity_score > 50
        else "MEDIUM"
        if complexity_score > 20
        else "LOW"
    )
    findings.append(
        f"\n- 决策复杂度: {complexity_score} ({level}) "
        f"— {'需要 MCTS 多路径探索' if level in ('HIGH', 'CRITICAL') else '简单决策树即可'}"
    )
    if problem:
        findings.append(f"- 待解决问题: {problem[:200]}")

    return "\n".join(findings)


def build_mcts_decision_report(problem: str, scan_evidence: str = "") -> str:
    """Build a deterministic multi-path pruning report."""
    complexity = extract_mcts_complexity(scan_evidence)
    high_risk = complexity in {"HIGH", "CRITICAL"}
    path_a_score = 8 if not high_risk else 7
    path_b_score = 6 if not high_risk else 8
    path_c_score = 5 if high_risk else 4
    winning = "Path B" if high_risk else "Path A"
    winning_name = "分阶段隔离改造" if high_risk else "最小可验证修复"
    return "\n".join(
        [
            "## MCTS 确定性多路径探索",
            f"- 问题：{problem}",
            f"- 复杂度等级：{complexity or 'UNKNOWN'}",
            "",
            "### Path A: 最小可验证修复",
            "- Approach: 只改动直接相关的最小代码路径，先补回归测试再修复。",
            "- Estimated effort: 小；适合低/中复杂度问题。",
            "- Pros: 变更面小，容易回滚，验证成本低。",
            "- Cons: 如果根因是结构性问题，可能只修症状。",
            "- Disaster simulation: 可能漏掉相邻调用路径；可能保留隐藏耦合。",
            f"- Score: {path_a_score}/10",
            "",
            "### Path B: 分阶段隔离改造",
            "- Approach: 先加观测和测试护栏，再把风险逻辑隔离到小模块中替换。",
            "- Estimated effort: 中；适合复杂度较高或影响面不清的问题。",
            "- Pros: 能降低爆炸半径，便于逐步验证。",
            "- Cons: 需要更多测试，短期代码改动更大。",
            "- Disaster simulation: 抽象边界选错会制造新耦合；迁移不完整会双轨不一致。",
            f"- Score: {path_b_score}/10",
            "",
            "### Path C: 全面重构",
            "- Approach: 重新设计相关模块边界，一次性清理历史债务。",
            "- Estimated effort: 高；只适合已有充分测试和明确架构目标时使用。",
            "- Pros: 能解决系统性债务。",
            "- Cons: 回归风险最大，容易偏离当前问题。",
            "- Disaster simulation: 大范围行为漂移；测试覆盖不足时无法证明正确性。",
            f"- Score: {path_c_score}/10",
            "",
            "### Pruning Decision",
            f"- Path A score: {path_a_score}/10 → {'KEEP' if path_a_score >= 6 else 'PRUNE'}",
            f"- Path B score: {path_b_score}/10 → {'KEEP' if path_b_score >= 6 else 'PRUNE'}",
            f"- Path C score: {path_c_score}/10 → {'KEEP' if path_c_score >= 6 else 'PRUNE'}",
            "",
            f"### Winning Path: {winning} — {winning_name}",
            "- Why this path wins: 当前复杂度证据与变更风险最匹配。",
            "- Implementation plan: 复现问题 → 补测试/观测 → 实施最小变更 → targeted 验证。",
            "- Validation: 覆盖原始失败路径、至少一个边界输入、一个正常路径。",
            "- Backtracking trigger: targeted 测试无法覆盖影响面，或修复需要跨越多个无测试模块。",
            "",
            "### Regression Guard",
            "- 添加能复现该问题的回归测试，并在失败消息中保留业务语义。",
            "- 对高风险路径加入日志或指标，确保线上退化可见。",
        ]
    )


def extract_mcts_complexity(scan_evidence: str) -> str:
    """Extract complexity label from MCTS scan evidence."""
    match = re.search(r"决策复杂度:\s*\d+\s*\((\w+)\)", scan_evidence)
    return match.group(1) if match else ""
