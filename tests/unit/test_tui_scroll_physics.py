from __future__ import annotations

import pytest
from textual.events import MouseScrollDown, MouseScrollUp
from textual.widgets import Static

from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tui.app import (
    TUI_POINTER_SCROLL_LINES,
    ChatPanel,
    NaumiApp,
)


@pytest.mark.asyncio
async def test_tui_pointer_scroll_moves_exactly_one_line_per_event() -> None:
    engine = AgentEngine(AppConfig())
    app = NaumiApp(engine)

    assert TUI_POINTER_SCROLL_LINES == 1.0
    assert app.scroll_sensitivity_y == TUI_POINTER_SCROLL_LINES

    async with app.run_test(size=(100, 30)) as pilot:
        chat = app.query_one(ChatPanel)
        await chat.mount(
            *(Static(f"scroll fixture {index}\n" * 3) for index in range(50))
        )
        await pilot.pause()
        assert chat.max_scroll_y > 2

        chat.scroll_home(animate=False)
        await pilot.pause()
        assert chat.scroll_target_y == 0

        await pilot._post_mouse_events(
            [MouseScrollDown],
            widget=chat,
            offset=(1, 1),
        )
        await pilot.pause()
        assert chat.scroll_target_y == 1

        await pilot._post_mouse_events(
            [MouseScrollUp],
            widget=chat,
            offset=(1, 1),
        )
        await pilot.pause()
        assert chat.scroll_target_y == 0
