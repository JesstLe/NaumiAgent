"""UI-14.2c task QuickOpen authority and shared slash path tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.clipboard import strip_ansi
from naumi_agent.tasks.models import Task, TaskStatus
from naumi_agent.ui.task_panel import TaskViewItem
from naumi_agent.ui.task_quick_open import (
    search_terminal_tasks,
    terminal_task_template,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads(
    (PROJECT_ROOT / "tests/fixtures/ui14/task-quick-open-golden.json").read_text(
        encoding="utf-8"
    )
)


def _items() -> tuple[TaskViewItem, ...]:
    return tuple(TaskViewItem(**item) for item in GOLDEN["items"])


def test_task_quick_open_shared_golden_ranking_and_safe_template() -> None:
    items = _items()

    assert [item.task_id for item in search_terminal_tasks(items, "")] == GOLDEN[
        "expected_empty_order"
    ]
    result = search_terminal_tasks(items, GOLDEN["expected_localized_query"])
    assert result[0].task_id == GOLDEN["expected_localized_result"]
    assert terminal_task_template(result[0]) == GOLDEN["expected_template"]
    assert all(item.task_id != "unsafe;id" for item in search_terminal_tasks(items, ""))
    with pytest.raises(ValueError, match="limit"):
        search_terminal_tasks(items, "", limit=51)
    with pytest.raises(ValueError, match="安全"):
        terminal_task_template(items[-1])
    assert len(search_terminal_tasks(items * 100, "", limit=50)) == 50


class _TaskStore:
    async def list_tasks(self) -> list[Task]:
        return [
            Task(
                id="task-2",
                session_id="session-1",
                subject="等待验证",
                description="",
                status=TaskStatus.BLOCKED,
                owner="reviewer",
            )
        ]


class _Engine:
    task_store = _TaskStore()
    subagent_manager = None
    background_runner = None
    task_runner = None


@pytest.mark.asyncio
async def test_shared_tasks_detail_command_opens_typed_task() -> None:
    output = await execute_slash_command(_Engine(), "/tasks detail task-2")

    assert "Detail" in output
    assert "ID: task-2" in output
    assert "等待验证" in output


@pytest.mark.asyncio
async def test_shared_tasks_detail_command_rejects_ambiguous_arguments() -> None:
    output = await execute_slash_command(_Engine(), "/tasks detail")

    assert "用法: /tasks [detail <id>]" in strip_ansi(output)
