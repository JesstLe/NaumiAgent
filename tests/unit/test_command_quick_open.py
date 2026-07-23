"""UI-14.2 Textual multi-provider QuickOpen interaction tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input

from naumi_agent.agent_control import AgentControlSnapshot, AgentDescriptor
from naumi_agent.config.settings import AppConfig
from naumi_agent.memory.session import Session
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tasks.models import Task, TaskStatus
from naumi_agent.tui.agent_control import AgentControlScreen
from naumi_agent.tui.app import NaumiApp
from naumi_agent.tui.command_quick_open import CommandQuickOpenScreen
from naumi_agent.ui.workspace_file_index import WorkspaceFileIndex


class _QuickOpenTaskStore:
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


class _QuickOpenAgentControl:
    async def snapshot(self) -> AgentControlSnapshot:
        return AgentControlSnapshot(
            schema_version=2,
            session_id="",
            revision=3,
            generated_at="2026-07-23T00:00:00+00:00",
            agents=(
                AgentDescriptor(
                    name="Explore Worker",
                    description="探索项目",
                    kind="dynamic",
                    state="running",
                    task_count=1,
                    model_tier="fast",
                    capabilities=("explore",),
                    tools=("glob",),
                    permission_level="read_only",
                ),
            ),
        )


def test_agent_control_initial_target_rejects_unsafe_or_oversized_names() -> None:
    with pytest.raises(ValueError, match="可显示字符"):
        AgentControlScreen(object(), initial_id="bad\nagent")
    with pytest.raises(ValueError, match="可显示字符"):
        AgentControlScreen(object(), initial_id="a" * 201)


@pytest.mark.asyncio
async def test_tui_quick_open_cancels_or_fills_without_submitting() -> None:
    app = NaumiApp(AgentEngine(AppConfig()))

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#msg-input", Input)
        composer.value = "保留草稿"

        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, CommandQuickOpenScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert composer.value == "保留草稿"
        assert app._agent_busy is False  # noqa: SLF001

        await pilot.press("ctrl+p")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CommandQuickOpenScreen)
        query = screen.query_one("#command-quick-open-query", Input)
        query.value = "wr"
        await pilot.pause()
        assert screen._results[0].command == "/write"  # noqa: SLF001

        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, CommandQuickOpenScreen)
        assert composer.value.startswith("/write ")
        assert "<path>" in composer.value
        assert app._agent_busy is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_tui_quick_open_ranks_recent_submitted_command_first() -> None:
    app = NaumiApp(AgentEngine(AppConfig()))

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#msg-input", Input)
        composer.value = "/h"
        composer.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert app._recent_commands[0] == "/help"  # noqa: SLF001
        await pilot.press("ctrl+p")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CommandQuickOpenScreen)
        assert screen._results[0].command == "/help"  # noqa: SLF001
        assert "最近" in screen._render_entry(screen._results[0])  # noqa: SLF001
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_tui_quick_open_switches_to_typed_tasks_and_only_fills() -> None:
    engine = AgentEngine(AppConfig())
    engine.task_store = _QuickOpenTaskStore()
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#msg-input", Input)
        composer.value = "保留草稿"
        composer.focus()

        await pilot.press("ctrl+p")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CommandQuickOpenScreen)
        await pilot.press("tab")
        for _ in range(20):
            if screen._results:  # noqa: SLF001
                break
            await pilot.pause(0.05)

        assert screen._provider == "tasks"  # noqa: SLF001
        assert screen._results[0].task_id == "task-2"  # noqa: SLF001
        assert "任务 QuickOpen" in str(
            screen.query_one("#command-quick-open-title").render()
        )
        await pilot.press("enter")
        await pilot.pause()

        assert composer.value == "/tasks detail task-2"
        assert app._agent_busy is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_tui_quick_open_switches_to_workspace_sessions_and_only_fills() -> None:
    engine = AgentEngine(AppConfig())
    session = Session(
        id="quick-session",
        title="可恢复会话",
        workspace_root=str(engine.workspace_root),
        messages=[{"role": "user", "content": "不应显示的正文"}],
    )
    await engine.session_store.save(session)
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#msg-input", Input)
        await pilot.press("ctrl+p")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CommandQuickOpenScreen)
        await pilot.press("tab")
        await pilot.press("tab")
        for _ in range(20):
            if screen._results:  # noqa: SLF001
                break
            await pilot.pause(0.05)

        assert screen._provider == "sessions"  # noqa: SLF001
        assert screen._results[0].session_id == "quick-session"  # noqa: SLF001
        assert "不应显示的正文" not in screen._render_entry(screen._results[0])  # noqa: SLF001
        await pilot.press("enter")
        await pilot.pause()

        assert composer.value == "/load quick-session"
        assert app._agent_busy is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_tui_quick_open_searches_workspace_files_and_only_fills_read(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "中文 file.py").write_text("print('ok')", encoding="utf-8")
    engine = AgentEngine(AppConfig())
    engine.workspace_file_index = WorkspaceFileIndex(tmp_path)
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#msg-input", Input)
        composer.value = "保留草稿"
        await pilot.press("ctrl+p")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CommandQuickOpenScreen)
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.press("tab")
        for _ in range(40):
            if screen._results:  # noqa: SLF001
                break
            await pilot.pause(0.05)

        assert screen._provider == "files"  # noqa: SLF001
        assert screen._results[0].path == "src/中文 file.py"  # noqa: SLF001
        assert "/private/" not in screen._render_entry(screen._results[0])  # noqa: SLF001
        await pilot.press("enter")
        await pilot.pause()

        assert composer.value == "/read 'src/中文 file.py'"
        assert app._agent_busy is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_tui_quick_open_deep_links_authoritative_agent_without_execution() -> None:
    engine = AgentEngine(AppConfig())
    engine.agent_control = _QuickOpenAgentControl()
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#msg-input", Input)
        await pilot.press("ctrl+p")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CommandQuickOpenScreen)
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.press("tab")
        await pilot.press("tab")
        for _ in range(20):
            if screen._results:  # noqa: SLF001
                break
            await pilot.pause(0.05)

        assert screen._provider == "agents"  # noqa: SLF001
        assert screen._results[0].name == "Explore Worker"  # noqa: SLF001
        await pilot.press("enter")
        await pilot.pause()
        assert composer.value == "/agents agent 'Explore Worker'"
        assert app._agent_busy is False  # noqa: SLF001

        composer.focus()
        await pilot.press("enter")
        for _ in range(20):
            if isinstance(app.screen, AgentControlScreen) and app.screen.snapshot:
                break
            await pilot.pause(0.05)
        assert isinstance(app.screen, AgentControlScreen)
        assert app.screen.selected_id == "Explore Worker"
        assert app._agent_busy is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_tui_quick_open_searches_authoritative_pages_and_only_fills() -> None:
    app = NaumiApp(AgentEngine(AppConfig()))

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#msg-input", Input)
        composer.value = "保留草稿"
        await pilot.press("ctrl+p")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CommandQuickOpenScreen)

        for _ in range(5):
            await pilot.press("tab")
            await pilot.pause()

        assert screen._provider == "pages"  # noqa: SLF001
        query = screen.query_one("#command-quick-open-query", Input)
        query.value = "目标"
        await pilot.pause()
        assert screen._results[0].page_id == "goals"  # noqa: SLF001
        assert "页面 QuickOpen" in str(
            screen.query_one("#command-quick-open-title").render()
        )
        assert "/chat" not in {
            entry.command for entry in screen._page_entries  # noqa: SLF001
        }

        await pilot.press("enter")
        await pilot.pause()
        assert composer.value == "/goal"
        assert app._agent_busy is False  # noqa: SLF001
