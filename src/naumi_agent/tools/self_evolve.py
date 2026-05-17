"""自我进化 — 反思循环评估修改效果，决定采纳或回滚."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)

_MAX_REFLECTIVE_ROUNDS = 3


@dataclass
class QualityMetrics:
    """Quality metrics for a source file."""

    lines_of_code: int = 0
    function_count: int = 0
    class_count: int = 0
    docstring_coverage: float = 0.0
    type_annotation_ratio: float = 0.0
    error_handling_score: float = 0.0
    import_count: int = 0
    cyclomatic_complexity: int = 0
    lint_errors: int = 0
    test_passed: bool = True

    @property
    def composite_score(self) -> float:
        """Compute a composite quality score (0-100).

        Higher is better. Weighted combination of individual metrics.
        """
        score = 50.0  # baseline

        # Reward docstring coverage (0-15 points)
        score += docstring_coverage_weight(self.docstring_coverage)

        # Reward type annotations (0-10 points)
        score += self.type_annotation_ratio * 10.0

        # Reward error handling (0-10 points)
        score += self.error_handling_score * 10.0

        # Penalize high complexity (0-5 penalty)
        if self.function_count > 0:
            avg_complexity = self.cyclomatic_complexity / max(self.function_count, 1)
            score -= min(avg_complexity * 2.0, 5.0)

        # Penalize lint errors (up to -15 points)
        score -= min(self.lint_errors * 5.0, 15.0)

        # Bonus for passing tests (+10)
        if self.test_passed:
            score += 10.0

        return max(0.0, min(100.0, score))


def docstring_coverage_weight(coverage: float) -> float:
    return coverage * 15.0


@dataclass
class EvolutionStep:
    """A single step in the evolution history."""

    target_file: str
    description: str
    status: str  # proposed, applied, evaluated, adopted, reverted
    step_id: str = ""
    round_number: int = 1
    diff: str = ""
    before_metrics: dict[str, Any] = field(default_factory=dict)
    after_metrics: dict[str, Any] = field(default_factory=dict)
    score_delta: float = 0.0
    decision_reason: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.step_id:
            self.step_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


# In-memory evolution history (persists within a session).
_evolution_history: list[EvolutionStep] = []


def _reset_history() -> None:
    """Clear evolution history (for testing)."""
    _evolution_history.clear()


def get_evolution_history() -> list[EvolutionStep]:
    """Return the evolution history."""
    return list(_evolution_history)


def measure_quality(source: str, file_path: Path | None = None) -> QualityMetrics:
    """Measure quality metrics from source code text.

    Args:
        source: The Python source code to measure.
        file_path: Optional path for running lint/tests.

    Returns:
        QualityMetrics with computed values.
    """
    lines = source.split("\n")
    code_lines = [
        ln for ln in lines
        if ln.strip() and not ln.strip().startswith("#")
    ]

    metrics = QualityMetrics(
        lines_of_code=len(code_lines),
    )

    # Count functions and classes
    func_defs = re.findall(r"^\s*(?:async\s+)?def\s+(\w+)", source, re.MULTILINE)
    class_defs = re.findall(r"^\s*class\s+(\w+)", source, re.MULTILINE)
    metrics.function_count = len(func_defs)
    metrics.class_count = len(class_defs)

    # Docstring coverage
    if metrics.function_count + metrics.class_count > 0:
        docstrings = re.findall(
            r'(?:async\s+)?def\s+\w+.*?:\s*"""', source, re.DOTALL
        )
        class_docstrings = re.findall(
            r'class\s+\w+.*?:\s*"""', source, re.DOTALL
        )
        total_docstrings = len(docstrings) + len(class_docstrings)
        total_defs = metrics.function_count + metrics.class_count
        metrics.docstring_coverage = total_docstrings / total_defs

    # Type annotation ratio — fraction of defs with -> annotation
    if func_defs:
        annotated = re.findall(
            r"(?:async\s+)?def\s+\w+\s*\([^)]*\)\s*->", source, re.MULTILINE
        )
        metrics.type_annotation_ratio = len(annotated) / metrics.function_count

    # Error handling score — fraction of try blocks relative to functions
    if metrics.function_count > 0:
        try_count = len(re.findall(r"\btry\s*:", source))
        metrics.error_handling_score = min(
            try_count / metrics.function_count, 1.0
        )

    # Import count
    metrics.import_count = len(re.findall(r"^\s*(?:from\s+\S+\s+)?import\s+", source, re.MULTILINE))

    # Cyclomatic complexity — count decision points
    decisions = re.findall(
        r"\b(if|elif|for|while|and|or|except)\b", source
    )
    metrics.cyclomatic_complexity = len(decisions) + 1

    # Lint errors (if file_path provided)
    if file_path and file_path.exists():
        metrics.lint_errors = _count_ruff_errors(file_path)

    return metrics


def _count_ruff_errors(file_path: Path) -> int:
    """Count ruff lint errors for a file."""
    import subprocess
    import sys

    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(file_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return 0
        # Count lines that look like error reports
        error_lines = [
            ln for ln in result.stdout.strip().split("\n")
            if re.match(r"^.*:\d+:\d+: [A-Z]\d+", ln)
        ]
        return len(error_lines)
    except Exception:
        return 0


def compare_metrics(
    before: QualityMetrics,
    after: QualityMetrics,
) -> dict[str, Any]:
    """Compare before/after quality metrics.

    Returns:
        Dict with delta values and overall assessment.
    """
    deltas = {
        "lines_of_code": after.lines_of_code - before.lines_of_code,
        "function_count": after.function_count - before.function_count,
        "docstring_coverage": round(
            after.docstring_coverage - before.docstring_coverage, 3
        ),
        "type_annotation_ratio": round(
            after.type_annotation_ratio - before.type_annotation_ratio, 3
        ),
        "error_handling_score": round(
            after.error_handling_score - before.error_handling_score, 3
        ),
        "cyclomatic_complexity": after.cyclomatic_complexity
        - before.cyclomatic_complexity,
        "lint_errors": after.lint_errors - before.lint_errors,
    }

    score_delta = round(after.composite_score - before.composite_score, 2)

    return {
        "before_score": round(before.composite_score, 2),
        "after_score": round(after.composite_score, 2),
        "score_delta": score_delta,
        "deltas": deltas,
        "improved": score_delta > 0,
        "before_summary": _metrics_summary(before),
        "after_summary": _metrics_summary(after),
    }


def _metrics_summary(m: QualityMetrics) -> str:
    """One-line summary of metrics."""
    return (
        f"LOC={m.lines_of_code} funcs={m.function_count} "
        f"docs={m.docstring_coverage:.0%} types={m.type_annotation_ratio:.0%} "
        f"complexity={m.cyclomatic_complexity} lint={m.lint_errors}"
    )


def reflective_evaluate(
    target_file: str,
    original_content: str,
    new_content: str,
    description: str,
    round_number: int = 1,
) -> dict[str, Any]:
    """Run reflective evaluation on a modification.

    Measures quality before and after, compares, and decides:
    - adopt: quality improved
    - revert: quality degraded
    - iterate: ambiguous — needs another round

    Returns:
        Evaluation result dict.
    """
    # Measure before
    file_path = None
    try:
        from naumi_agent.tools.self_modify import _resolve_target_path

        file_path = _resolve_target_path(target_file)
    except (ValueError, FileNotFoundError):
        pass

    before_metrics = measure_quality(original_content, file_path)

    # Write new content to a temp location for measurement
    import tempfile

    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", encoding="utf-8", delete=False,
    ) as tmp:
        tmp.write(new_content)
        tmp_path = Path(tmp.name)

    try:
        after_metrics = measure_quality(new_content, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    # Compare
    comparison = compare_metrics(before_metrics, after_metrics)

    # Decide
    score_delta = comparison["score_delta"]
    lint_delta = comparison["deltas"]["lint_errors"]

    if score_delta > 0 and lint_delta <= 0:
        decision = "adopt"
        reason = (
            f"质量评分提升 {score_delta:+.1f} 分，"
            f"lint 错误变化 {lint_delta:+d}。建议采纳。"
        )
    elif score_delta < -5 or lint_delta > 0:
        decision = "revert"
        reason = (
            f"质量评分下降 {score_delta:+.1f} 分，"
            f"lint 错误增加 {lint_delta:+d}。建议回滚。"
        )
    else:
        decision = "iterate"
        reason = (
            f"质量评分变化 {score_delta:+.1f} 分，"
            f"效果不明确。建议迭代优化。"
        )

    # Record evolution step
    step = EvolutionStep(
        step_id="",
        target_file=target_file,
        description=description,
        status="evaluated",
        round_number=round_number,
        before_metrics={
            "composite_score": before_metrics.composite_score,
            "lines_of_code": before_metrics.lines_of_code,
            "function_count": before_metrics.function_count,
            "docstring_coverage": before_metrics.docstring_coverage,
            "lint_errors": before_metrics.lint_errors,
        },
        after_metrics={
            "composite_score": after_metrics.composite_score,
            "lines_of_code": after_metrics.lines_of_code,
            "function_count": after_metrics.function_count,
            "docstring_coverage": after_metrics.docstring_coverage,
            "lint_errors": after_metrics.lint_errors,
        },
        score_delta=score_delta,
        decision_reason=reason,
    )
    _evolution_history.append(step)

    return {
        "decision": decision,
        "comparison": comparison,
        "reason": reason,
        "step_id": step.step_id,
        "round": round_number,
    }


def format_evolution_report(
    eval_result: dict[str, Any],
    modify_result: dict[str, Any] | None = None,
) -> str:
    """Format evolution results into a human-readable report."""
    parts: list[str] = ["## 🧬 自我进化报告"]

    comparison = eval_result["comparison"]
    decision = eval_result["decision"]
    reason = eval_result["reason"]

    # Decision header
    decision_icons = {
        "adopt": "✅ 采纳",
        "revert": "🔄 回滚",
        "iterate": "🔄 迭代",
    }
    icon = decision_icons.get(decision, "❓ 未知")
    parts.append(f"**决策**: {icon}")
    parts.append(f"**原因**: {reason}")
    parts.append("")

    # Score comparison
    parts.append("### 质量评分")
    parts.append(
        f"- 修改前: {comparison['before_score']:.1f}/100"
    )
    parts.append(
        f"- 修改后: {comparison['after_score']:.1f}/100"
    )
    parts.append(
        f"- 变化: {comparison['score_delta']:+.1f} 分"
    )
    parts.append("")

    # Detailed deltas
    parts.append("### 指标对比")
    deltas = comparison["deltas"]
    for name, delta in deltas.items():
        if delta != 0:
            positive_metrics = (
                "docstring_coverage",
                "type_annotation_ratio",
                "error_handling_score",
            )
            negative_metrics = ("lint_errors", "cyclomatic_complexity")
            if (name in positive_metrics and delta > 0) or (
                name in negative_metrics and delta < 0
            ):
                icon_char = "📈"
            elif delta != 0:
                icon_char = "📉"
            else:
                icon_char = "➡️"
            parts.append(f"- {icon_char} {name}: {delta:+}")
    parts.append("")

    # Modify result if available
    if modify_result and modify_result.get("status") == "applied":
        parts.append("### 验证结果")
        for check_name, check_result in modify_result.get("validation", {}).items():
            icon_str = "✅" if check_result["passed"] else "❌"
            status_str = "通过" if check_result["passed"] else "失败"
            parts.append(f"- {icon_str} {check_name}: {status_str}")

    return "\n".join(parts)


def run_evolution_cycle(
    target_file: str,
    original_content: str,
    new_content: str,
    description: str,
    current_round: int = 1,
    max_rounds: int = _MAX_REFLECTIVE_ROUNDS,
) -> dict[str, Any]:
    """Run a full evolution cycle: modify → validate → evaluate → decide.

    If evaluation says "iterate" and rounds remain, returns iteration hint.
    Caller (LLM or CLI) generates the next proposal and calls again.

    Returns:
        Dict with evaluation result and next action.
    """
    # Step 1: Reflective evaluation
    eval_result = reflective_evaluate(
        target_file=target_file,
        original_content=original_content,
        new_content=new_content,
        description=description,
        round_number=current_round,
    )

    decision = eval_result["decision"]

    # Step 2: If adopt — already applied by self_modify, just confirm
    if decision == "adopt":
        return {
            "action": "commit",
            "eval_result": eval_result,
            "message": "修改质量提升，建议提交。",
        }

    # Step 3: If revert — rollback
    if decision == "revert":
        return {
            "action": "rollback",
            "eval_result": eval_result,
            "message": "修改质量下降，已回滚。",
        }

    # Step 4: If iterate — suggest next round if rounds remain
    if current_round < max_rounds:
        return {
            "action": "iterate",
            "eval_result": eval_result,
            "message": (
                f"效果不明确 (第 {current_round}/{max_rounds} 轮)，"
                "建议调整修改方案后重试。"
            ),
            "next_round": current_round + 1,
        }

    # Rounds exhausted — default to revert
    return {
        "action": "rollback",
        "eval_result": eval_result,
        "message": f"迭代 {max_rounds} 轮后效果仍不明确，建议回滚。",
    }


class SelfEvolveTool(Tool):
    """自我进化 — 反思循环评估修改效果."""

    @property
    def name(self) -> str:
        return "self_evolve"

    @property
    def description(self) -> str:
        return (
            "自我进化 — 评估自身代码修改的效果。"
            "对比修改前后的质量指标（代码质量、类型注解、文档覆盖率等），"
            "通过反思循环决定采纳、回滚或继续迭代。"
            "配合 self_modify 工具使用，形成完整的自我进化闭环。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target_file": {
                    "type": "string",
                    "description": "被修改的文件（相对路径，如 tools/analysis.py）",
                },
                "original_content": {
                    "type": "string",
                    "description": "修改前的完整文件内容",
                },
                "new_content": {
                    "type": "string",
                    "description": "修改后的完整文件内容",
                },
                "description": {
                    "type": "string",
                    "description": "修改说明",
                },
                "round": {
                    "type": "integer",
                    "description": "当前迭代轮次（从 1 开始）",
                },
            },
            "required": ["target_file", "original_content", "new_content", "description"],
        }

    async def execute(
        self,
        *,
        target_file: str,
        original_content: str,
        new_content: str,
        description: str,
        round: int = 1,
        **kwargs: Any,
    ) -> str:
        cycle_result = run_evolution_cycle(
            target_file=target_file,
            original_content=original_content,
            new_content=new_content,
            description=description,
            current_round=round,
        )

        report = format_evolution_report(cycle_result["eval_result"])
        report += f"\n\n**下一步**: {cycle_result['message']}"

        return report
