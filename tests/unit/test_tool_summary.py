"""Tests for tool-specific summary extractors."""

from __future__ import annotations

import pytest

from naumi_agent.ui.tool_summary import (
    ToolCardSummary,
    format_card_detail,
    format_card_title,
    summarize_tool_result,
    summarize_tool_start,
)


class TestToolStartSummaries:

    def test_bash_run_shows_command(self) -> None:
        summary = summarize_tool_start("bash_run", {"command": "ls -la"})
        assert summary.icon == "🖥️"
        assert summary.primary_arg == "ls -la"
        assert summary.status == "running"

    def test_file_write_shows_path(self) -> None:
        summary = summarize_tool_start(
            "file_write", {"file_path": "/tmp/test.py"}
        )
        assert summary.icon == "📝"
        assert "test.py" in summary.primary_arg

    def test_web_search_shows_query(self) -> None:
        summary = summarize_tool_start(
            "web_search", {"query": "python asyncio tutorial"}
        )
        assert summary.icon == "🔍"
        assert "asyncio" in summary.primary_arg

    def test_unknown_tool_uses_default_icon(self) -> None:
        summary = summarize_tool_start("custom_tool_123", {})
        assert summary.icon == "⚙️"
        assert summary.tool_name == "custom_tool_123"

    def test_args_as_json_string(self) -> None:
        summary = summarize_tool_start(
            "bash_run", '{"command": "echo hello"}'
        )
        assert summary.primary_arg == "echo hello"

    def test_long_primary_arg_truncated(self) -> None:
        summary = summarize_tool_start(
            "bash_run", {"command": "x" * 200}
        )
        assert len(summary.primary_arg) <= 53  # 50 + "…"
        assert summary.primary_arg.endswith("…")


class TestToolResultSummaries:

    def test_file_write_summary(self) -> None:
        content = "line1\nline2\nline3\nline4\nline5"
        summary = summarize_tool_result(
            "file_write",
            status="success",
            duration_ms=100,
            content=content,
            args_raw={"file_path": "/tmp/test.py"},
        )
        assert summary.file_path == "/tmp/test.py"
        assert summary.line_count == 5
        assert "写入" in summary.output_summary

    def test_file_edit_diff_summary(self) -> None:
        diff = (
            "--- a/test.py\n"
            "+++ b/test.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            "+import sys\n"
            " import json\n"
        )
        summary = summarize_tool_result(
            "file_edit",
            status="success",
            duration_ms=50,
            content=diff,
            args_raw={"file_path": "/tmp/test.py"},
        )
        assert summary.file_path == "/tmp/test.py"
        assert summary.hunk_count == 1
        assert summary.additions == 1
        assert summary.deletions == 0
        assert "+1/-0" in summary.output_summary

    def test_file_read_summary(self) -> None:
        content = "line1\nline2\nline3"
        summary = summarize_tool_result(
            "file_read",
            status="success",
            duration_ms=30,
            content=content,
            args_raw={"file_path": "/tmp/readme.md"},
        )
        assert summary.file_path == "/tmp/readme.md"
        assert "读取" in summary.output_summary

    def test_bash_run_success(self) -> None:
        summary = summarize_tool_result(
            "bash_run",
            status="success",
            duration_ms=200,
            content="hello world\n",
            args_raw={"command": "echo hello"},
        )
        assert summary.command == "echo hello"
        assert "hello world" in summary.output_summary

    def test_bash_run_error(self) -> None:
        summary = summarize_tool_result(
            "bash_run",
            status="error",
            duration_ms=10,
            content="command not found\nexit code 127",
            args_raw={"command": "badcmd"},
            error="command not found",
        )
        assert summary.command == "badcmd"
        assert summary.exit_code == 127
        assert summary.error_summary

    def test_todo_write_summary(self) -> None:
        summary = summarize_tool_result(
            "todo_write",
            status="success",
            duration_ms=5,
            content="updated",
            args_raw={
                "todos": [
                    {"id": 1, "status": "completed"},
                    {"id": 2, "status": "in_progress"},
                    {"id": 3, "status": "pending"},
                ],
            },
        )
        assert summary.todo_total == 3
        assert summary.todo_done == 1
        assert "1/3" in summary.output_summary

    def test_web_search_summary(self) -> None:
        content = (
            "1. Result one\n"
            "2. Result two\n"
            "3. Result three\n"
        )
        summary = summarize_tool_result(
            "web_search",
            status="success",
            duration_ms=1500,
            content=content,
            args_raw={"query": "python tutorial"},
        )
        assert summary.query == "python tutorial"
        assert summary.result_count == 3
        assert "3" in summary.output_summary

    def test_web_fetch_summary(self) -> None:
        summary = summarize_tool_result(
            "web_fetch",
            status="success",
            duration_ms=800,
            content="line1\nline2\nline3",
            args_raw={"url": "https://example.com"},
        )
        assert summary.url == "https://example.com"
        assert "获取" in summary.output_summary

    def test_generic_tool_summary(self) -> None:
        summary = summarize_tool_result(
            "custom_tool",
            status="success",
            duration_ms=100,
            content="short output",
        )
        assert "short output" in summary.output_summary

    def test_error_only_summary(self) -> None:
        summary = summarize_tool_result(
            "bash_run",
            status="error",
            duration_ms=0,
            content=None,
            error="Permission denied",
        )
        assert summary.error_summary == "Permission denied"

    def test_no_content_no_error(self) -> None:
        summary = summarize_tool_result(
            "some_tool",
            status="success",
            duration_ms=50,
            content=None,
        )
        assert summary.output_summary == ""


class TestCardFormatting:

    def test_card_title_with_icon(self) -> None:
        summary = ToolCardSummary(
            tool_name="bash_run",
            icon="🖥️",
            primary_arg="ls -la",
            status="running",
        )
        title = format_card_title(summary)
        assert "🖥️" in title
        assert "bash_run" in title
        assert "ls -la" in title

    def test_card_detail_output(self) -> None:
        summary = ToolCardSummary(
            output_summary="写入 100 行 / 2.5KB"
        )
        assert format_card_detail(summary) == "写入 100 行 / 2.5KB"

    def test_card_detail_error(self) -> None:
        summary = ToolCardSummary(
            error_summary="Permission denied"
        )
        assert format_card_detail(summary) == "Permission denied"

    def test_card_detail_empty(self) -> None:
        summary = ToolCardSummary()
        assert format_card_detail(summary) == ""


class TestSummaryImmutability:

    def test_frozen_dataclass(self) -> None:
        summary = ToolCardSummary(tool_name="test")
        with pytest.raises(AttributeError):
            summary.tool_name = "changed"  # type: ignore[mut]
