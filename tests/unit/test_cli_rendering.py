"""CLI rendering tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import naumi_agent.main as main_module
from naumi_agent.main import (
    _capture,
    _cli_event_factory,
    _format_context_compacted,
    _format_permission_bubble,
    _format_recovery_event,
    _format_runtime_notification,
    _format_todo_bar,
    _print_tool_output,
    _render_result,
    _show_cli_status,
    _StreamingMarkdownHighlighter,
    _tool_label,
)


class FakeCLI:
    def __init__(self) -> None:
        self.live: list[str] = []
        self.output: list[str] = []
        self.status = ""
        self.todo_status = ""

    def append_live(self, text: str) -> None:
        self.live.append(text)

    def append_output(self, text: str) -> None:
        self.output.append(text)

    def finalize_live(self) -> None:
        self.output.extend(self.live)
        self.live.clear()

    def set_status(self, text: str) -> None:
        self.status = text

    def set_todo_status(self, text: str | None) -> None:
        self.todo_status = text or ""


class FakeRouter:
    def resolve_model(self, _tier: str) -> str:
        return "test-model"


class FakeUsage:
    total_input_tokens = 10
    total_output_tokens = 20
    total_cost_usd = 0.01
    turns = 1
    cache_tokens = 0


class FakeEngine:
    router = FakeRouter()
    usage = FakeUsage()
    workspace_root = "/tmp/workspace"

    def get_context_info(self) -> dict[str, int]:
        return {"used": 3000, "window": 12000, "percentage": 25}

    def get_budget_info(self) -> dict[str, float]:
        return {"used_usd": 0.01, "max_usd": 5.0}


def test_tool_label_uses_english_tool_id() -> None:
    label = _tool_label("memory_recall", '{"query": "project"}')

    assert "memory_recall" in label
    assert "召回记忆" not in label
    assert "project" in label


def test_fenced_diff_tool_output_renders_diff_body() -> None:
    content = "✅ 已编辑 /tmp/a.py\n\n```diff\n--- before\n+++ after\n@@\n-old\n+new\n```"

    rendered = _capture(lambda: _print_tool_output("file_edit", content))

    assert "已编辑" in rendered
    assert "-old" in rendered
    assert "+new" in rendered
    assert "```diff" not in rendered
    assert "tool output · file_edit" in rendered


def test_cli_status_updates_fixed_status_not_output(monkeypatch) -> None:
    cli = FakeCLI()
    monkeypatch.setattr("naumi_agent.main._get_git_info", lambda: {"branch": "", "dirty": False})

    _show_cli_status(cli, FakeEngine())

    assert "test-model" in cli.status
    assert "工作区: /tmp/workspace" in cli.status
    assert cli.output == []


def test_fullscreen_result_omits_environment_stats(monkeypatch) -> None:
    monkeypatch.setattr("naumi_agent.main._get_git_info", lambda: {"branch": "main", "dirty": True})
    result = SimpleNamespace(
        status="completed",
        error=None,
        response="完成",
        usage=FakeUsage(),
    )

    rendered = _capture(
        lambda: _render_result(
            main_module.console,
            result,
            skip_response=True,
            model="test-model",
            engine=FakeEngine(),
            show_environment_stats=False,
        )
    )

    assert "轮次" in rendered
    assert "上下文" not in rendered
    assert "预算" not in rendered
    assert "main" not in rendered


@pytest.mark.asyncio
async def test_fullscreen_cli_tool_end_includes_tool_output() -> None:
    cli = FakeCLI()
    handler = _cli_event_factory(cli)

    await handler(
        "tool_end",
        {
            "name": "file_edit",
            "status": "success",
            "duration_ms": 12,
            "content": "```diff\n--- before\n+++ after\n@@\n-old\n+new\n```",
        },
    )

    text = "".join(cli.live)
    assert "file_edit" in text
    assert "-old" in text
    assert "+new" in text
    assert "tool output · file_edit" in text


def test_context_compacted_rendering_includes_preserved_state_and_warnings() -> None:
    rendered = _format_context_compacted({
        "before": 64,
        "after": 8,
        "archived_tool_results": 2,
        "preserved_sections": ["todo", "team_protocol"],
        "warnings": ["有 1 个未完成/阻塞 todo"],
    })

    assert "64" in rendered
    assert "8" in rendered
    assert "todo" in rendered
    assert "team_protocol" in rendered
    assert "未完成/阻塞" in rendered
    assert "大型工具结果" in rendered


def test_recovery_event_rendering_includes_reason_and_action() -> None:
    rendered = _format_recovery_event({
        "reason": "prompt_too_long",
        "action": "reactive_compact_retry",
        "phase": "completed",
        "before": 80,
        "after": 9,
    })

    assert "prompt_too_long" in rendered
    assert "reactive_compact_retry" in rendered
    assert "80" in rendered
    assert "9" in rendered


def test_permission_bubble_rendering_includes_agent_tool_and_reason() -> None:
    rendered = _format_permission_bubble({
        "agent_name": "coder",
        "tool_name": "bash_run",
        "status": "needs_confirmation",
        "reason": "该工具需要用户确认",
    })

    assert "coder" in rendered
    assert "bash_run" in rendered
    assert "needs_confirmation" in rendered
    assert "需要用户确认" in rendered


def test_runtime_notification_rendering_includes_title_source_and_preview() -> None:
    rendered = _format_runtime_notification({
        "title": "后台任务通知",
        "source": "background",
        "count": 2,
        "preview": "任务ID：bg_0001；状态：已完成",
    })

    assert "后台任务通知" in rendered
    assert "background" in rendered
    assert "×2" in rendered
    assert "bg_0001" in rendered


def test_todo_bar_shows_current_open_task_and_clears_when_complete() -> None:
    active = _format_todo_bar({
        "count": 3,
        "open_count": 1,
        "completed_count": 2,
        "items": [{"id": "3", "status": "in_progress", "subject": "正在补测试"}],
    })
    done = _format_todo_bar({
        "count": 3,
        "open_count": 0,
        "completed_count": 3,
        "items": [],
    })

    assert "todo: 2/3 完成" in active
    assert "正在补测试" in active
    assert done == ""


@pytest.mark.asyncio
async def test_fullscreen_cli_runtime_notification_is_visible() -> None:
    cli = FakeCLI()
    handler = _cli_event_factory(cli)

    await handler(
        "runtime_notification",
        {
            "title": "调度提醒",
            "source": "schedule",
            "count": 1,
            "preview": "内容：检查测试结果",
        },
    )

    text = "".join(cli.live)
    assert "调度提醒" in text
    assert "schedule" in text
    assert "检查测试结果" in text


@pytest.mark.asyncio
async def test_fullscreen_cli_task_snapshot_updates_sticky_todo_bar() -> None:
    cli = FakeCLI()
    handler = _cli_event_factory(cli)

    await handler(
        "task_snapshot",
        {
            "source": "todo_write",
            "count": 2,
            "open_count": 1,
            "completed_count": 1,
            "items": [{"id": "2", "status": "pending", "subject": "补测试"}],
            "summary": "summary should not be appended",
        },
    )

    assert "补测试" in cli.todo_status
    assert cli.live == []

    await handler(
        "task_snapshot",
        {
            "source": "todo_write",
            "count": 2,
            "open_count": 0,
            "completed_count": 2,
            "items": [],
        },
    )

    assert cli.todo_status == ""


def test_streaming_markdown_highlighter_colors_fenced_python() -> None:
    highlighter = _StreamingMarkdownHighlighter()

    rendered = "".join([
        highlighter.feed("说明\n```py"),
        highlighter.feed("thon\nfrom __future__ import annotations\n"),
        highlighter.feed("class CircuitBreaker:\n"),
        highlighter.feed("```\n结束"),
        highlighter.flush(),
    ])

    assert "说明" in rendered
    assert "结束" in rendered
    assert "from" in rendered
    assert "CircuitBreaker" in rendered
    assert "\x1b[38;5;" in rendered


@pytest.mark.asyncio
async def test_fullscreen_cli_streaming_tokens_highlight_code_blocks() -> None:
    cli = FakeCLI()
    handler = _cli_event_factory(cli)

    await handler("response_start", {})
    await handler("token", {"content": "```python\n"})
    await handler("token", {"content": "class CircuitBreaker:\n"})
    await handler("token", {"content": "    pass\n```\n"})
    await handler("response_end", {})

    text = "".join([*cli.output, *cli.live])
    assert "CircuitBreaker" in text
    assert "\x1b[38;5;" in text


@pytest.mark.asyncio
async def test_fullscreen_cli_permission_confirmation_returns_choice() -> None:
    cli_layout = pytest.importorskip("naumi_agent.cli.layout")
    cli_app = cli_layout.CLIApp
    cli = cli_app()

    task = asyncio.create_task(
        cli.confirm_permission(
            {
                "tool_name": "code_execute",
                "reason": "该工具需要用户确认。",
                "arguments": {"code": "print('ok')"},
                "risk_level": "high",
                "permission_mode": "moderate",
            }
        )
    )
    for _ in range(20):
        if cli._pending_permission is not None:
            break
        await asyncio.sleep(0)

    cli._resolve_pending_permission("allow")
    choice = await task

    assert choice == "allow"
    text = "".join(cli._live)
    assert "权限确认" in text
    assert "code_execute" in text
