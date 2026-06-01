"""CLI rendering tests."""

from __future__ import annotations

import pytest

from naumi_agent.main import (
    _capture,
    _cli_event_factory,
    _format_context_compacted,
    _print_tool_output,
    _show_cli_status,
    _tool_label,
)


class FakeCLI:
    def __init__(self) -> None:
        self.live: list[str] = []
        self.output: list[str] = []
        self.status = ""

    def append_live(self, text: str) -> None:
        self.live.append(text)

    def append_output(self, text: str) -> None:
        self.output.append(text)

    def finalize_live(self) -> None:
        self.output.extend(self.live)
        self.live.clear()

    def set_status(self, text: str) -> None:
        self.status = text


class FakeRouter:
    def resolve_model(self, _tier: str) -> str:
        return "test-model"


class FakeUsage:
    total_input_tokens = 10
    total_output_tokens = 20


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


def test_cli_status_updates_fixed_status_not_output(monkeypatch) -> None:
    cli = FakeCLI()
    monkeypatch.setattr("naumi_agent.main._get_git_info", lambda: {"branch": "", "dirty": False})

    _show_cli_status(cli, FakeEngine())

    assert "test-model" in cli.status
    assert "工作区: /tmp/workspace" in cli.status
    assert cli.output == []


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


def test_context_compacted_rendering_includes_preserved_state_and_warnings() -> None:
    rendered = _format_context_compacted({
        "before": 64,
        "after": 8,
        "preserved_sections": ["todo", "team_protocol"],
        "warnings": ["有 1 个未完成/阻塞 todo"],
    })

    assert "64" in rendered
    assert "8" in rendered
    assert "todo" in rendered
    assert "team_protocol" in rendered
    assert "未完成/阻塞" in rendered
