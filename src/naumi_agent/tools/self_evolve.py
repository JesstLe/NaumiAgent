"""自我进化 — 反思循环评估修改效果，决定采纳或回滚."""

from __future__ import annotations

import json
import logging
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from naumi_agent.tools.base import Tool, ToolMetadata

logger = logging.getLogger(__name__)

_MAX_REFLECTIVE_ROUNDS = 3
MAX_EVOLUTION_CONTENT_CHARS = 200_000
MAX_EVOLUTION_DESCRIPTION_CHARS = 2_000


def _normalize_evolution_inputs(
    target_file: Any,
    original_content: Any,
    new_content: Any,
    description: Any,
    round_number: Any,
    apply_decision: Any,
) -> tuple[str, str, str, str, int, bool]:
    """Validate public self-evolution inputs before scoring or rollback."""
    if not isinstance(target_file, str) or not target_file.strip():
        raise ValueError("target_file 不能为空，且必须是字符串。")
    target_file = target_file.strip()
    if not target_file.endswith(".py"):
        raise ValueError("target_file 必须指向 .py 文件。")
    target_path = PurePosixPath(target_file)
    if (
        target_path.is_absolute()
        or "\\" in target_file
        or any(part == ".." for part in target_path.parts)
    ):
        raise ValueError("target_file 不能是绝对路径或包含路径越界片段。")

    if not isinstance(original_content, str):
        raise ValueError("original_content 必须是字符串。")
    if len(original_content) > MAX_EVOLUTION_CONTENT_CHARS:
        raise ValueError(
            "original_content 过大，当前上限为 "
            f"{MAX_EVOLUTION_CONTENT_CHARS} 个字符。"
        )

    if not isinstance(new_content, str):
        raise ValueError("new_content 必须是字符串。")
    if len(new_content) > MAX_EVOLUTION_CONTENT_CHARS:
        raise ValueError(
            "new_content 过大，当前上限为 "
            f"{MAX_EVOLUTION_CONTENT_CHARS} 个字符。"
        )

    if not isinstance(description, str) or not description.strip():
        raise ValueError("description 不能为空，且必须是字符串。")
    description = description.strip()
    if len(description) > MAX_EVOLUTION_DESCRIPTION_CHARS:
        raise ValueError(
            "description 过长，当前上限为 "
            f"{MAX_EVOLUTION_DESCRIPTION_CHARS} 个字符。"
        )

    round_number = _normalize_round_number(round_number)
    if round_number < 1 or round_number > _MAX_REFLECTIVE_ROUNDS:
        raise ValueError(f"round 必须在 1 到 {_MAX_REFLECTIVE_ROUNDS} 之间。")

    apply_decision = _normalize_apply_decision(apply_decision)

    return (
        target_file,
        original_content,
        new_content,
        description,
        round_number,
        apply_decision,
    )


def _normalize_round_number(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("round 必须是整数。")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError("round 必须是整数。")


def _normalize_apply_decision(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    if value is None:
        return False
    raise ValueError("apply_decision 必须是布尔值。")


def _normalize_return_json(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    if value is None:
        return False
    raise ValueError("return_json 必须是布尔值。")


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
        found = re.search(r"Found\s+(\d+)\s+errors?", result.stdout)
        if found:
            return int(found.group(1))
        # Count lines that look like ruff error headers. Ruff's output format
        # has changed across versions, so support both legacy path:line:col
        # reports and newer code-first reports.
        error_lines = [
            ln for ln in result.stdout.strip().split("\n")
            if re.match(r"^.*:\d+:\d+: [A-Z]\d+", ln)
            or re.match(r"^[A-Z]\d{3}\b", ln)
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


def _measure_quality_in_temp(source: str) -> QualityMetrics:
    """Measure source quality, including lint, without reading target file state."""
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", encoding="utf-8", delete=False,
    ) as tmp:
        tmp.write(source)
        tmp_path = Path(tmp.name)
    try:
        return measure_quality(source, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


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
    before_metrics = _measure_quality_in_temp(original_content)
    after_metrics = _measure_quality_in_temp(new_content)

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


def _mark_history_step(step_id: str, status: str, reason: str = "") -> None:
    """Update a recorded evolution step status."""
    for step in _evolution_history:
        if step.step_id == step_id:
            step.status = status
            if reason:
                step.decision_reason = reason
            return


def apply_evolution_decision(
    target_file: str,
    original_content: str,
    new_content: str,
    eval_result: dict[str, Any],
) -> dict[str, Any]:
    """Apply a reflective decision when it is safe to do so.

    Only rollback writes to disk, and only when the current file content still
    exactly matches the evaluated new_content. This prevents overwriting user
    edits that happened after the evaluation.
    """
    decision = eval_result["decision"]
    step_id = str(eval_result["step_id"])
    if decision == "adopt":
        _mark_history_step(step_id, "adopted")
        return {
            "applied": True,
            "action": "adopted",
            "message": "已记录采纳决策；提交仍需由调用方显式执行。",
        }
    if decision == "iterate":
        _mark_history_step(step_id, "iteration_requested")
        return {
            "applied": False,
            "action": "iteration_requested",
            "message": "已记录迭代决策；等待下一轮修改方案。",
        }

    try:
        from naumi_agent.tools.self_modify import (
            _is_modifiable_file,
            _is_protected_file,
            _resolve_target_path,
        )

        file_path = _resolve_target_path(target_file)
    except (ValueError, FileNotFoundError) as exc:
        _mark_history_step(step_id, "rollback_failed", str(exc))
        return {
            "applied": False,
            "action": "rollback_failed",
            "message": f"无法解析回滚目标：{exc}",
        }

    if _is_protected_file(file_path) or not _is_modifiable_file(file_path):
        message = "目标文件不在允许回滚范围，已拒绝写回。"
        _mark_history_step(step_id, "rollback_failed", message)
        return {"applied": False, "action": "rollback_failed", "message": message}

    try:
        current_content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        _mark_history_step(step_id, "rollback_failed", str(exc))
        return {
            "applied": False,
            "action": "rollback_failed",
            "message": f"读取当前文件失败：{exc}",
        }

    if current_content != new_content:
        message = "当前文件内容已不同于本轮评估的新内容，为避免覆盖后续改动，拒绝自动回滚。"
        _mark_history_step(step_id, "rollback_blocked", message)
        return {"applied": False, "action": "rollback_blocked", "message": message}

    try:
        file_path.write_text(original_content, encoding="utf-8")
    except OSError as exc:
        _mark_history_step(step_id, "rollback_failed", str(exc))
        return {
            "applied": False,
            "action": "rollback_failed",
            "message": f"写回原始内容失败：{exc}",
        }

    _mark_history_step(step_id, "reverted")
    return {
        "applied": True,
        "action": "reverted",
        "message": "已确认当前内容匹配本轮评估结果，并写回原始内容。",
    }


def format_evolution_report(
    eval_result: dict[str, Any],
    modify_result: dict[str, Any] | None = None,
    apply_result: dict[str, Any] | None = None,
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

    if apply_result is not None:
        parts.append("")
        parts.append("### 执行闭环")
        icon_str = "✅" if apply_result.get("applied") else "⚠️"
        parts.append(f"- {icon_str} {apply_result.get('message', '')}")

    return "\n".join(parts)


def run_evolution_cycle(
    target_file: str,
    original_content: str,
    new_content: str,
    description: str,
    current_round: int = 1,
    max_rounds: int = _MAX_REFLECTIVE_ROUNDS,
    apply_decision: bool = False,
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
    apply_result = (
        apply_evolution_decision(
            target_file=target_file,
            original_content=original_content,
            new_content=new_content,
            eval_result=eval_result,
        )
        if apply_decision
        else None
    )

    # Step 2: If adopt — already applied by self_modify, just confirm
    if decision == "adopt":
        return {
            "action": "commit",
            "eval_result": eval_result,
            "apply_result": apply_result,
            "message": "修改质量提升，建议提交。",
        }

    # Step 3: If revert — rollback
    if decision == "revert":
        if apply_result and apply_result.get("action") == "reverted":
            message = "修改质量下降，已回滚。"
        elif apply_result:
            message = f"修改质量下降，但回滚未执行：{apply_result.get('message', '未知原因')}"
        else:
            message = "修改质量下降，建议回滚；当前尚未执行回滚。"
        return {
            "action": "rollback",
            "eval_result": eval_result,
            "apply_result": apply_result,
            "message": message,
        }

    # Step 4: If iterate — suggest next round if rounds remain
    if current_round < max_rounds:
        return {
            "action": "iterate",
            "eval_result": eval_result,
            "apply_result": apply_result,
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
        "apply_result": apply_result,
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
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            user_facing_name="自我进化",
            search_hint="评估源码修改 质量指标 反思循环 安全回滚",
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
                "apply_decision": {
                    "type": "boolean",
                    "description": "是否执行安全闭环：采纳仅记录，回滚仅在内容精确匹配时写回。",
                    "default": False,
                },
                "return_json": {
                    "type": "boolean",
                    "description": "是否返回结构化 JSON，供 CLI/TUI 在同一工具执行链中读取决策。",
                    "default": False,
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
        apply_decision: bool = False,
        return_json: bool = False,
        **kwargs: Any,
    ) -> str:
        try:
            return_json = _normalize_return_json(return_json)
            apply_decision = _normalize_apply_decision(apply_decision)
            (
                target_file,
                original_content,
                new_content,
                description,
                round,
                apply_decision,
            ) = _normalize_evolution_inputs(
                target_file,
                original_content,
                new_content,
                description,
                round,
                apply_decision,
            )
        except ValueError as e:
            report = "\n".join(
                [
                    "## 自我进化报告",
                    "**状态**: ❌ 已拒绝",
                    f"**原因**: {e}",
                ]
            )
            if return_json is True:
                return json.dumps(
                    {
                        "report": report,
                        "cycle_result": {
                            "action": "rejected",
                            "message": str(e),
                            "target_file": target_file
                            if isinstance(target_file, str)
                            else "",
                        },
                    },
                    ensure_ascii=False,
                )
            return report

        cycle_result = run_evolution_cycle(
            target_file=target_file,
            original_content=original_content,
            new_content=new_content,
            description=description,
            current_round=round,
            apply_decision=apply_decision,
        )

        try:
            report = format_evolution_report(
                cycle_result["eval_result"],
                apply_result=cycle_result.get("apply_result"),
            )
            report += f"\n\n**下一步**: {cycle_result['message']}"
        except (KeyError, TypeError, ValueError) as e:
            message = f"自我进化循环结果格式错误: {e}"
            report = "\n".join(
                [
                    "## 自我进化报告",
                    "**状态**: ❌ 已拒绝",
                    f"**原因**: {message}",
                ]
            )
            cycle_result = {
                "action": "rejected",
                "message": message,
                "target_file": target_file,
            }

        if return_json:
            return json.dumps(
                {
                    "report": report,
                    "cycle_result": cycle_result,
                },
                ensure_ascii=False,
            )

        return report
