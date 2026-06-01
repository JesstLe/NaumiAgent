"""Tests for file/diff summary renderers."""

from __future__ import annotations

from naumi_agent.ui.file_summary_renderer import (
    _color_diff,
    _format_diff_preview,
    format_file_edit_summary,
    format_file_read_summary,
    format_file_write_summary,
    format_tool_output_preview,
    render_tool_summary_card,
)
from naumi_agent.ui.tool_summary import ToolCardSummary


class TestFileWriteSummary:

    def test_success_summary(self) -> None:
        summary = ToolCardSummary(
            tool_name="file_write",
            file_path="/tmp/test.py",
            line_count=100,
            byte_count=4500,
            duration_ms=200,
            status="success",
        )
        text = format_file_write_summary(summary)
        assert "写入成功" in text
        assert "/tmp/test.py" in text
        assert "100" in text
        assert "4.4KB" in text
        assert "200ms" in text

    def test_failure_summary(self) -> None:
        summary = ToolCardSummary(
            tool_name="file_write",
            file_path="/tmp/test.py",
            status="error",
            error_summary="Permission denied",
        )
        text = format_file_write_summary(summary)
        assert "写入失败" in text
        assert "Permission denied" in text


class TestFileEditSummary:

    def test_diff_stats_summary(self) -> None:
        summary = ToolCardSummary(
            tool_name="file_edit",
            file_path="/tmp/test.py",
            hunk_count=3,
            additions=15,
            deletions=8,
            duration_ms=50,
            status="success",
        )
        text = format_file_edit_summary(summary)
        assert "编辑成功" in text
        assert "3 hunk" in text
        assert "+15" in text
        assert "-8" in text

    def test_edit_error(self) -> None:
        summary = ToolCardSummary(
            tool_name="file_edit",
            file_path="/tmp/test.py",
            status="error",
            error_summary="File not found",
        )
        text = format_file_edit_summary(summary)
        assert "编辑失败" in text
        assert "File not found" in text


class TestFileReadSummary:

    def test_read_summary(self) -> None:
        summary = ToolCardSummary(
            tool_name="file_read",
            file_path="/tmp/readme.md",
            line_count=50,
            duration_ms=30,
            status="success",
        )
        text = format_file_read_summary(summary)
        assert "读取完成" in text
        assert "50" in text


class TestDiffPreview:

    def test_short_diff_full_display(self) -> None:
        diff_lines = [
            "--- a/test.py",
            "+++ b/test.py",
            "@@ -1,3 +1,4 @@",
            " import os",
            "+import sys",
            " import json",
        ]
        text = _format_diff_preview(diff_lines)
        assert "import sys" in text
        assert "未展示" not in text

    def test_long_diff_truncated(self) -> None:
        lines = [
            f"+line {i}" for i in range(50)
        ]
        text = _format_diff_preview(lines)
        assert "未展示" in text
        assert "10" in text  # 50 - 40 = 10 hidden

    def test_color_diff_additions_green(self) -> None:
        lines = ["+added line"]
        text = _color_diff(lines)
        assert "32" in text  # green ANSI

    def test_color_diff_deletions_red(self) -> None:
        lines = ["-removed line"]
        text = _color_diff(lines)
        assert "31" in text  # red ANSI

    def test_color_diff_headers(self) -> None:
        lines = ["--- a/test.py", "+++ b/test.py", "@@ -1 +1 @@"]
        text = _color_diff(lines)
        assert "32" in text  # +++ green
        assert "31" in text  # --- red
        assert "36" in text  # @@ cyan


class TestToolOutputPreview:

    def test_short_output_full(self) -> None:
        text = format_tool_output_preview("bash_run", "hello\nworld")
        assert "hello" in text
        assert "未展示" not in text

    def test_long_output_truncated(self) -> None:
        content = "\n".join(f"line {i}" for i in range(100))
        text = format_tool_output_preview("bash_run", content, max_lines=20)
        assert "未展示" in text

    def test_empty_content(self) -> None:
        text = format_tool_output_preview("bash_run", "")
        assert text == ""

    def test_diff_content_detected(self) -> None:
        diff = "--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-old\n+new"
        text = format_tool_output_preview("file_edit", diff)
        # Should use diff preview path, not generic
        assert "old" in text or "new" in text


class TestDispatchCard:

    def test_dispatch_file_write(self) -> None:
        summary = ToolCardSummary(
            tool_name="file_write",
            status="success",
            file_path="/tmp/t.py",
        )
        text = render_tool_summary_card(summary)
        assert "写入成功" in text

    def test_dispatch_file_edit(self) -> None:
        summary = ToolCardSummary(
            tool_name="file_edit",
            status="success",
            file_path="/tmp/t.py",
        )
        text = render_tool_summary_card(summary)
        assert "编辑成功" in text

    def test_dispatch_file_read(self) -> None:
        summary = ToolCardSummary(
            tool_name="file_read",
            status="success",
            file_path="/tmp/t.py",
        )
        text = render_tool_summary_card(summary)
        assert "读取完成" in text

    def test_dispatch_generic_output(self) -> None:
        summary = ToolCardSummary(
            tool_name="bash_run",
            output_summary="command completed",
        )
        text = render_tool_summary_card(summary)
        assert "command completed" in text

    def test_dispatch_generic_error(self) -> None:
        summary = ToolCardSummary(
            tool_name="bash_run",
            error_summary="Permission denied",
        )
        text = render_tool_summary_card(summary)
        assert "Permission denied" in text

    def test_dispatch_no_info(self) -> None:
        summary = ToolCardSummary(tool_name="unknown")
        text = render_tool_summary_card(summary)
        assert text == ""
