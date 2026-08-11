"""Focused Textual TUI dispatch coverage for Pursuit retention preview."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from naumi_agent.tools.base import ToolResult
from naumi_agent.tui.app import ChatPanel, NaumiApp, StatusBar


class _MountTarget:
    def __init__(self) -> None:
        self.mounted: list[Any] = []

    def mount(self, widget: Any) -> None:
        self.mounted.append(widget)


class _TuiFacade:
    def __init__(self) -> None:
        self.chat = _MountTarget()
        self.status = SimpleNamespace(status_text="")
        self.engine = SimpleNamespace(tool_registry={
            "pursuit_terminal_outbox_retention_preview": object(),
            "pursuit_terminal_outbox_retention_admission": object(),
        })
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def query_one(self, target: type[Any]) -> Any:
        if target is ChatPanel:
            return self.chat
        if target is StatusBar:
            return self.status
        raise AssertionError(f"unexpected query target: {target}")

    async def _execute_registered_tool(
        self,
        tool_name: str,
        **arguments: Any,
    ) -> ToolResult:
        self.calls.append((tool_name, arguments))
        return ToolResult(
            call_id="tui-retention-preview",
            status="success",
            content="只读候选预演，不是删除授权。",
        )


@pytest.mark.asyncio
async def test_tui_pursue_outbox_routes_retention_preview() -> None:
    tui = _TuiFacade()

    await NaumiApp._run_pursue_meta(
        tui,  # type: ignore[arg-type]
        "outbox",
        "retention-preview --retention-days 45 --limit 5 --scan-limit 10 "
        "--assessed-at 2026-08-11T08:00:00+08:00",
    )

    assert tui.calls == [(
        "pursuit_terminal_outbox_retention_preview",
        {
            "retention_days": 45,
            "limit": 5,
            "scan_limit": 10,
            "assessed_at": "2026-08-11T08:00:00+08:00",
        },
    )]
    assert tui.status.status_text == "就绪"
    assert len(tui.chat.mounted) == 1


@pytest.mark.asyncio
async def test_tui_pursue_outbox_routes_retention_admission() -> None:
    tui = _TuiFacade()
    digest = "a" * 64

    await NaumiApp._run_pursue_meta(
        tui,  # type: ignore[arg-type]
        "outbox",
        f"retention-admit ptorpv_{digest[:24]} {digest} "
        "--retention-days 45 --limit 5 --scan-limit 10 "
        "--assessed-at 2026-08-11T08:00:00+08:00",
    )

    assert tui.calls == [(
        "pursuit_terminal_outbox_retention_admission",
        {
            "preview_id": f"ptorpv_{digest[:24]}",
            "preview_sha256": digest,
            "retention_days": 45,
            "limit": 5,
            "scan_limit": 10,
            "assessed_at": "2026-08-11T08:00:00+08:00",
        },
    )]
    assert tui.status.status_text == "就绪"
    assert len(tui.chat.mounted) == 1
