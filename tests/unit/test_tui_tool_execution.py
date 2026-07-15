"""Tests for the TUI's authoritative tool execution helper."""

from __future__ import annotations

import json

import pytest

from naumi_agent.tools.base import ToolCall, ToolResult
from naumi_agent.tui import app as tui_app
from naumi_agent.tui.app import NaumiApp


class _TuiEngineFake:
    def __init__(self) -> None:
        self.calls: list[tuple[ToolCall, str | None]] = []
        self.tool = _DirectToolTrap()
        self.tool_registry = {"worktree_status": self.tool}
        self.next_result = ToolResult(
            call_id="placeholder",
            status="success",
            content="执行完成。",
        )

    def set_permission_confirmer(self, _callback: object) -> None:
        pass

    def set_user_interaction_handler(self, _callback: object) -> None:
        pass

    async def execute_tool(
        self,
        tool_call: ToolCall,
        *,
        agent_name: str | None = None,
    ) -> ToolResult:
        self.calls.append((tool_call, agent_name))
        return ToolResult(
            call_id=tool_call.id,
            status=self.next_result.status,
            content=self.next_result.content,
        )


class _DirectToolTrap:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        raise AssertionError("TUI 不得直接调用 Tool.execute")


class _ChatFake:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def mount(self, message: str) -> None:
        self.messages.append(message)


class _StatusFake:
    status_text = ""


class _InputFake:
    focused = False

    def focus(self) -> None:
        self.focused = True


class _InputBarFake:
    def __init__(self) -> None:
        self.input = _InputFake()

    def query_one(self, *_args: object) -> _InputFake:
        return self.input


class _TuiCommandHarness:
    def __init__(self, engine: _TuiEngineFake) -> None:
        self.engine = engine
        self.chat = _ChatFake()
        self.status = _StatusFake()
        self.input_bar = _InputBarFake()

    def query_one(self, widget_type: type[object]) -> object:
        if widget_type is tui_app.ChatPanel:
            return self.chat
        if widget_type is tui_app.StatusBar:
            return self.status
        if widget_type is tui_app.InputBar:
            return self.input_bar
        raise AssertionError(f"未预期的组件查询: {widget_type}")

    async def _execute_registered_tool(
        self,
        tool_name: str,
        **arguments: object,
    ) -> ToolResult:
        return await NaumiApp._execute_registered_tool(  # type: ignore[arg-type]
            self,
            tool_name,
            **arguments,
        )


@pytest.mark.asyncio
async def test_tui_tool_helper_uses_public_engine_facade_with_unique_calls() -> None:
    engine = _TuiEngineFake()
    app = NaumiApp(engine)  # type: ignore[arg-type]

    first = await app._execute_registered_tool(
        "worktree_create",
        name="演示",
        task_id="任务-1",
    )
    second = await app._execute_registered_tool("worktree_status", name="演示")

    assert first.status == "success"
    assert second.status == "success"
    assert len(engine.calls) == 2
    first_call, first_agent = engine.calls[0]
    second_call, second_agent = engine.calls[1]
    assert first_agent == second_agent == "tui"
    assert first_call.id != second_call.id
    assert first_call.name == "worktree_create"
    assert json.loads(first_call.arguments) == {"name": "演示", "task_id": "任务-1"}


@pytest.mark.asyncio
async def test_tui_tool_helper_preserves_engine_failure_result() -> None:
    engine = _TuiEngineFake()
    engine.next_result = ToolResult(
        call_id="placeholder",
        status="error",
        content="权限策略拒绝了本次操作。",
    )
    app = NaumiApp(engine)  # type: ignore[arg-type]

    result = await app._execute_registered_tool("worktree_remove", name="演示")

    assert result.status == "error"
    assert result.content == "权限策略拒绝了本次操作。"


@pytest.mark.asyncio
async def test_tui_worktree_handler_renders_engine_failure_without_direct_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _TuiEngineFake()
    engine.next_result = ToolResult(
        call_id="placeholder",
        status="error",
        content="权限策略拒绝了本次操作。",
    )
    harness = _TuiCommandHarness(engine)
    monkeypatch.setattr(
        tui_app,
        "Markdown",
        lambda content, **_kwargs: content,
    )

    handler = NaumiApp._run_worktree_command.__wrapped__
    await handler(harness, "status demo")  # type: ignore[arg-type]

    assert engine.tool.calls == []
    assert harness.chat.messages == ["**Worktree 命令失败**: 权限策略拒绝了本次操作。"]
    assert harness.status.status_text == "就绪"
    assert harness.input_bar.input.focused is True
