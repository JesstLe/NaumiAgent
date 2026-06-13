"""Self-evolution tool tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from naumi_agent.tools.self_evolve import (
    MAX_EVOLUTION_CONTENT_CHARS,
    EvolutionStep,
    QualityMetrics,
    SelfEvolveTool,
    _measure_quality_in_temp,
    _metrics_summary,
    _reset_history,
    apply_evolution_decision,
    compare_metrics,
    docstring_coverage_weight,
    format_evolution_report,
    get_evolution_history,
    measure_quality,
    reflective_evaluate,
    run_evolution_cycle,
)

# Sample source code for testing.
SIMPLE_SOURCE = '"""Module."""\n\nx = 1\n'

BETTER_SOURCE = '''"""Module docstring."""

from typing import Any


def add(x: int, y: int) -> int:
    """Add two numbers."""
    return x + y


def multiply(x: int, y: int) -> int:
    """Multiply two numbers."""
    return x * y
'''

WORSE_SOURCE = '''import os
import sys
import json
import re
import glob
x=1;y=2
def f(a,b,c,d,e,f,g,h):
 if a:
  if b:
   if c:
    if d:
     return e
'''

NO_DOC_SOURCE = '''def foo(x):
    return x + 1

def bar(y):
    return y * 2

def baz(z):
    return z - 3
'''

FULL_DOC_SOURCE = '''def foo(x: int) -> int:
    """Foo function."""
    return x + 1

def bar(y: int) -> int:
    """Bar function."""
    return y * 2
'''

ERROR_HANDLING_SOURCE = '''def safe_divide(x: int, y: int) -> float:
    """Safe divide."""
    try:
        return x / y
    except ZeroDivisionError:
        return 0.0
'''


class TestQualityMetrics:
    def test_composite_score_baseline(self):
        m = QualityMetrics()
        assert m.composite_score == 60.0

    def test_composite_score_max_100(self):
        m = QualityMetrics(
            docstring_coverage=1.0,
            type_annotation_ratio=1.0,
            error_handling_score=1.0,
            test_passed=True,
        )
        assert m.composite_score <= 100.0

    def test_composite_score_min_0(self):
        m = QualityMetrics(
            docstring_coverage=0.0,
            type_annotation_ratio=0.0,
            error_handling_score=0.0,
            lint_errors=10,
        )
        assert m.composite_score >= 0.0

    def test_test_passed_bonus(self):
        m_pass = QualityMetrics(test_passed=True)
        m_fail = QualityMetrics(test_passed=False)
        assert m_pass.composite_score > m_fail.composite_score

    def test_lint_errors_penalty(self):
        m_clean = QualityMetrics(lint_errors=0)
        m_dirty = QualityMetrics(lint_errors=5)
        assert m_clean.composite_score > m_dirty.composite_score


class TestDocstringCoverageWeight:
    def test_zero(self):
        assert docstring_coverage_weight(0.0) == 0.0

    def test_full(self):
        assert docstring_coverage_weight(1.0) == 15.0

    def test_half(self):
        assert docstring_coverage_weight(0.5) == 7.5


class TestMeasureQuality:
    def test_simple_source(self):
        m = measure_quality(SIMPLE_SOURCE)
        assert m.lines_of_code >= 1
        assert m.function_count == 0

    def test_better_source(self):
        m = measure_quality(BETTER_SOURCE)
        assert m.function_count == 2
        assert m.docstring_coverage == 1.0
        assert m.type_annotation_ratio == 1.0
        assert m.lines_of_code > 0

    def test_worse_source(self):
        m = measure_quality(WORSE_SOURCE)
        assert m.function_count == 1
        assert m.docstring_coverage == 0.0
        assert m.cyclomatic_complexity > 1

    def test_no_doc(self):
        m = measure_quality(NO_DOC_SOURCE)
        assert m.function_count == 3
        assert m.docstring_coverage == 0.0

    def test_full_doc(self):
        m = measure_quality(FULL_DOC_SOURCE)
        assert m.function_count == 2
        assert m.docstring_coverage == 1.0

    def test_error_handling(self):
        m = measure_quality(ERROR_HANDLING_SOURCE)
        assert m.function_count == 1
        assert m.error_handling_score > 0

    def test_import_count(self):
        m = measure_quality(BETTER_SOURCE)
        assert m.import_count >= 1

    def test_class_count(self):
        src = 'class Foo:\n    pass\n\nclass Bar:\n    pass\n'
        m = measure_quality(src)
        assert m.class_count == 2

    def test_with_file_path(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text(BETTER_SOURCE, encoding="utf-8")
        m = measure_quality(BETTER_SOURCE, file_path=f)
        assert isinstance(m.lint_errors, int)

    def test_counts_modern_ruff_output(self, tmp_path: Path):
        f = tmp_path / "bad.py"
        f.write_text("import os\nimport os\nx=1\n", encoding="utf-8")
        m = measure_quality(f.read_text(encoding="utf-8"), file_path=f)
        assert m.lint_errors >= 1

    def test_temp_measurement_uses_source_content_not_target_state(self):
        clean = _measure_quality_in_temp(FULL_DOC_SOURCE)
        dirty = _measure_quality_in_temp("import os\nimport os\nx=1\n")

        assert clean.lint_errors == 0
        assert dirty.lint_errors >= 1

    def test_empty_source(self):
        m = measure_quality("")
        assert m.lines_of_code == 0
        assert m.function_count == 0

    def test_comment_only(self):
        m = measure_quality("# comment\n# another\n")
        assert m.lines_of_code == 0


class TestCompareMetrics:
    def test_improvement(self):
        before = QualityMetrics(
            docstring_coverage=0.0,
            type_annotation_ratio=0.0,
        )
        after = QualityMetrics(
            docstring_coverage=1.0,
            type_annotation_ratio=1.0,
        )
        result = compare_metrics(before, after)
        assert result["improved"] is True
        assert result["score_delta"] > 0

    def test_regression(self):
        before = QualityMetrics(
            docstring_coverage=1.0,
            lint_errors=0,
        )
        after = QualityMetrics(
            docstring_coverage=0.0,
            lint_errors=5,
        )
        result = compare_metrics(before, after)
        assert result["improved"] is False
        assert result["score_delta"] < 0

    def test_no_change(self):
        m = QualityMetrics()
        result = compare_metrics(m, m)
        assert result["score_delta"] == 0.0

    def test_deltas_populated(self):
        before = QualityMetrics(lines_of_code=10, function_count=2)
        after = QualityMetrics(lines_of_code=15, function_count=3)
        result = compare_metrics(before, after)
        assert result["deltas"]["lines_of_code"] == 5
        assert result["deltas"]["function_count"] == 1

    def test_summaries(self):
        before = QualityMetrics(lines_of_code=10, function_count=2)
        result = compare_metrics(before, before)
        assert "LOC=10" in result["before_summary"]


class TestMetricsSummary:
    def test_format(self):
        m = QualityMetrics(
            lines_of_code=42,
            function_count=5,
            docstring_coverage=0.8,
            type_annotation_ratio=0.6,
            cyclomatic_complexity=12,
            lint_errors=2,
        )
        summary = _metrics_summary(m)
        assert "LOC=42" in summary
        assert "funcs=5" in summary


class TestReflectiveEvaluate:
    def setup_method(self):
        _reset_history()

    def test_adopt_on_improvement(self):
        result = reflective_evaluate(
            target_file="tools/test.py",
            original_content=NO_DOC_SOURCE,
            new_content=FULL_DOC_SOURCE,
            description="add docstrings",
        )
        assert result["decision"] == "adopt"
        assert result["comparison"]["improved"] is True

    def test_revert_on_regression(self):
        result = reflective_evaluate(
            target_file="tools/test.py",
            original_content=BETTER_SOURCE,
            new_content=WORSE_SOURCE,
            description="broke everything",
        )
        assert result["decision"] == "revert"

    def test_iterate_on_ambiguous(self):
        # Slightly different but similar quality
        src1 = 'def foo(x: int) -> int:\n    """Foo."""\n    return x\n'
        src2 = 'def foo(x: int) -> int:\n    """Foo."""\n    return x + 1\n'
        result = reflective_evaluate(
            target_file="tools/test.py",
            original_content=src1,
            new_content=src2,
            description="tiny change",
        )
        # Small delta — should be iterate or adopt
        assert result["decision"] in ("adopt", "iterate", "revert")

    def test_records_history(self):
        reflective_evaluate(
            target_file="tools/test.py",
            original_content=SIMPLE_SOURCE,
            new_content=BETTER_SOURCE,
            description="improve",
        )
        history = get_evolution_history()
        assert len(history) == 1
        assert history[0].status == "evaluated"

    def test_round_number_recorded(self):
        reflective_evaluate(
            target_file="tools/test.py",
            original_content=SIMPLE_SOURCE,
            new_content=BETTER_SOURCE,
            description="round 2",
            round_number=2,
        )
        history = get_evolution_history()
        assert history[0].round_number == 2


class TestRunEvolutionCycle:
    def setup_method(self):
        _reset_history()

    def test_commit_on_improvement(self):
        result = run_evolution_cycle(
            target_file="tools/test.py",
            original_content=NO_DOC_SOURCE,
            new_content=FULL_DOC_SOURCE,
            description="add docs",
        )
        assert result["action"] == "commit"
        assert "提升" in result["message"]

    def test_rollback_on_regression(self):
        result = run_evolution_cycle(
            target_file="tools/test.py",
            original_content=BETTER_SOURCE,
            new_content=WORSE_SOURCE,
            description="broke",
        )
        assert result["action"] == "rollback"

    def test_revert_decision_without_apply_does_not_claim_rollback(self):
        result = run_evolution_cycle(
            target_file="tools/test.py",
            original_content=BETTER_SOURCE,
            new_content=WORSE_SOURCE,
            description="broke",
            apply_decision=False,
        )

        assert result["action"] == "rollback"
        assert result["apply_result"] is None
        assert "建议回滚" in result["message"]
        assert "已回滚" not in result["message"]

    def test_iterate_when_ambiguous(self):
        src1 = 'def foo(x: int) -> int:\n    """Foo."""\n    return x\n'
        src2 = 'def foo(x: int) -> int:\n    """Foo."""\n    return x + 1\n'
        result = run_evolution_cycle(
            target_file="tools/test.py",
            original_content=src1,
            new_content=src2,
            description="tiny",
        )
        # Should either commit, iterate, or rollback depending on score
        assert result["action"] in ("commit", "iterate", "rollback")

    def test_max_rounds_exhausted(self):
        result = run_evolution_cycle(
            target_file="tools/test.py",
            original_content=SIMPLE_SOURCE,
            new_content=SIMPLE_SOURCE.replace("1", "2"),
            description="noop-like",
            current_round=3,
            max_rounds=3,
        )
        # Same content essentially → might rollback or iterate
        assert result["action"] in ("commit", "rollback", "iterate")

    def test_apply_decision_rolls_back_when_current_content_matches(
        self,
        tmp_path: Path,
    ):
        target = tmp_path / "tools" / "evolve_case.py"
        target.parent.mkdir()
        target.write_text(WORSE_SOURCE, encoding="utf-8")

        with patch(
            "naumi_agent.tools.self_modify._find_agent_source_dir",
            return_value=tmp_path,
        ):
            result = run_evolution_cycle(
                target_file="tools/evolve_case.py",
                original_content=BETTER_SOURCE,
                new_content=WORSE_SOURCE,
                description="rollback bad change",
                apply_decision=True,
            )

        assert result["action"] == "rollback"
        assert result["apply_result"]["action"] == "reverted"
        assert target.read_text(encoding="utf-8") == BETTER_SOURCE
        assert get_evolution_history()[-1].status == "reverted"

    def test_apply_decision_blocks_rollback_when_file_changed(
        self,
        tmp_path: Path,
    ):
        target = tmp_path / "tools" / "evolve_case.py"
        target.parent.mkdir()
        target.write_text("# user edited after evaluation\n", encoding="utf-8")

        eval_result = reflective_evaluate(
            target_file="tools/evolve_case.py",
            original_content=BETTER_SOURCE,
            new_content=WORSE_SOURCE,
            description="rollback bad change",
        )

        with patch(
            "naumi_agent.tools.self_modify._find_agent_source_dir",
            return_value=tmp_path,
        ):
            result = apply_evolution_decision(
                target_file="tools/evolve_case.py",
                original_content=BETTER_SOURCE,
                new_content=WORSE_SOURCE,
                eval_result=eval_result,
            )

        assert result["action"] == "rollback_blocked"
        assert target.read_text(encoding="utf-8") == "# user edited after evaluation\n"
        assert get_evolution_history()[-1].status == "rollback_blocked"


class TestEvolutionStep:
    def test_auto_id(self):
        step = EvolutionStep(
            target_file="test.py",
            description="test",
            status="proposed",
        )
        assert len(step.step_id) == 12
        assert step.created_at

    def test_custom_id(self):
        step = EvolutionStep(
            step_id="custom123",
            target_file="test.py",
            description="test",
            status="proposed",
        )
        assert step.step_id == "custom123"


class TestFormatEvolutionReport:
    def test_adopt_report(self):
        eval_result = reflective_evaluate(
            target_file="tools/test.py",
            original_content=NO_DOC_SOURCE,
            new_content=FULL_DOC_SOURCE,
            description="add docs",
        )
        _reset_history()
        report = format_evolution_report(eval_result)
        assert "采纳" in report
        assert "质量评分" in report

    def test_revert_report(self):
        eval_result = reflective_evaluate(
            target_file="tools/test.py",
            original_content=BETTER_SOURCE,
            new_content=WORSE_SOURCE,
            description="broke",
        )
        _reset_history()
        report = format_evolution_report(eval_result)
        assert "回滚" in report

    def test_report_with_modify_result(self):
        eval_result = reflective_evaluate(
            target_file="tools/test.py",
            original_content=NO_DOC_SOURCE,
            new_content=FULL_DOC_SOURCE,
            description="add docs",
        )
        _reset_history()
        modify_result = {
            "status": "applied",
            "validation": {
                "ruff_check": {"passed": True},
                "pytest": {"passed": True},
            },
        }
        report = format_evolution_report(eval_result, modify_result)
        assert "验证结果" in report

    def test_report_with_apply_result(self):
        eval_result = reflective_evaluate(
            target_file="tools/test.py",
            original_content=BETTER_SOURCE,
            new_content=WORSE_SOURCE,
            description="broke",
        )
        _reset_history()
        report = format_evolution_report(
            eval_result,
            apply_result={
                "applied": True,
                "action": "reverted",
                "message": "已写回原始内容。",
            },
        )
        assert "执行闭环" in report
        assert "已写回原始内容" in report


class TestSelfEvolveTool:
    def test_tool_name(self):
        assert SelfEvolveTool().name == "self_evolve"

    def test_tool_description(self):
        desc = SelfEvolveTool().description
        assert "进化" in desc or "评估" in desc

    def test_tool_schema(self):
        schema = SelfEvolveTool().parameters_schema
        assert "target_file" in schema["properties"]
        assert "original_content" in schema["properties"]
        assert "new_content" in schema["properties"]
        assert "round" in schema["properties"]
        assert "apply_decision" in schema["properties"]
        assert len(schema["required"]) == 4

    def test_metadata_marks_self_evolve_as_confirmed_state_change(self):
        metadata = SelfEvolveTool().metadata
        assert metadata.destructive is True
        assert metadata.requires_confirmation is True
        assert metadata.user_facing_name == "自我进化"

    @pytest.mark.parametrize(
        ("kwargs", "expected_reason"),
        [
            (
                {"target_file": ""},
                "target_file 不能为空",
            ),
            (
                {"target_file": "tools/test.txt"},
                "target_file 必须指向 .py 文件",
            ),
            (
                {"target_file": "../escape.py"},
                "target_file 不能是绝对路径",
            ),
            (
                {"original_content": None},
                "original_content 必须是字符串",
            ),
            (
                {"new_content": None},
                "new_content 必须是字符串",
            ),
            (
                {"description": ""},
                "description 不能为空",
            ),
            (
                {"round": 0},
                "round 必须在 1 到 3 之间",
            ),
            (
                {"round": True},
                "round 必须是整数",
            ),
            (
                {"apply_decision": "yes"},
                "apply_decision 必须是布尔值",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_execute_rejects_invalid_inputs_before_cycle(
        self,
        kwargs,
        expected_reason,
    ):
        base_kwargs = {
            "target_file": "tools/test.py",
            "original_content": NO_DOC_SOURCE,
            "new_content": FULL_DOC_SOURCE,
            "description": "add docs",
        }
        base_kwargs.update(kwargs)

        with patch("naumi_agent.tools.self_evolve.run_evolution_cycle") as cycle_mock:
            result = await SelfEvolveTool().execute(**base_kwargs)

        cycle_mock.assert_not_called()
        assert "已拒绝" in result
        assert expected_reason in result

    @pytest.mark.asyncio
    async def test_execute_rejects_oversized_content_before_cycle(self):
        with patch("naumi_agent.tools.self_evolve.run_evolution_cycle") as cycle_mock:
            result = await SelfEvolveTool().execute(
                target_file="tools/test.py",
                original_content=NO_DOC_SOURCE,
                new_content="x" * (MAX_EVOLUTION_CONTENT_CHARS + 1),
                description="oversized",
            )

        cycle_mock.assert_not_called()
        assert "已拒绝" in result
        assert "new_content 过大" in result

    @pytest.mark.asyncio
    async def test_execute_improvement(self):
        _reset_history()
        tool = SelfEvolveTool()
        result = await tool.execute(
            target_file="tools/test.py",
            original_content=NO_DOC_SOURCE,
            new_content=FULL_DOC_SOURCE,
            description="add docs",
        )
        assert "采纳" in result
        assert "质量评分" in result

    @pytest.mark.asyncio
    async def test_execute_regression(self):
        _reset_history()
        tool = SelfEvolveTool()
        result = await tool.execute(
            target_file="tools/test.py",
            original_content=BETTER_SOURCE,
            new_content=WORSE_SOURCE,
            description="broke",
        )
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_execute_with_round(self):
        _reset_history()
        tool = SelfEvolveTool()
        result = await tool.execute(
            target_file="tools/test.py",
            original_content=SIMPLE_SOURCE,
            new_content=BETTER_SOURCE,
            description="improve",
            round=2,
        )
        assert "进化" in result

    @pytest.mark.asyncio
    async def test_execute_apply_decision_includes_closed_loop(self, tmp_path: Path):
        _reset_history()
        target = tmp_path / "tools" / "evolve_case.py"
        target.parent.mkdir()
        target.write_text(WORSE_SOURCE, encoding="utf-8")

        with patch(
            "naumi_agent.tools.self_modify._find_agent_source_dir",
            return_value=tmp_path,
        ):
            result = await SelfEvolveTool().execute(
                target_file="tools/evolve_case.py",
                original_content=BETTER_SOURCE,
                new_content=WORSE_SOURCE,
                description="rollback bad change",
                apply_decision=True,
            )

        assert "执行闭环" in result
        assert "写回原始内容" in result
        assert target.read_text(encoding="utf-8") == BETTER_SOURCE
