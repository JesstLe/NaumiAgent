"""UI-14.2a Textual command QuickOpen interaction tests."""

from __future__ import annotations

import pytest
from textual.widgets import Input

from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tui.app import NaumiApp
from naumi_agent.tui.command_quick_open import CommandQuickOpenScreen


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
