"""UI-14.1 authoritative terminal command index tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from naumi_agent.cli.completer import COMMANDS_META
from naumi_agent.tui.app import InputBar
from naumi_agent.ui.command_index import (
    CommandArgumentSchema,
    TerminalCommandIndexEntry,
    build_terminal_command_index,
)


def test_new_ui_command_index_is_complete_deterministic_and_unique() -> None:
    first = build_terminal_command_index("new_ui")
    second = build_terminal_command_index("new_ui")

    assert first == second
    assert len(first) == len(COMMANDS_META) + 11
    assert len({item.command for item in first}) == len(first)
    assert all(item.schema_version == 1 for item in first)
    assert all(item.description for item in first)

    by_name = {item.command: item for item in first}
    assert by_name["/help"].aliases == ("/h",)
    assert by_name["/new"].aliases == ("/n",)
    assert by_name["/read"].permission_risk == "read_only"
    assert by_name["/write"].permission_risk == "workspace_write"
    assert by_name["/delete"].permission_risk == "destructive"
    assert by_name["/mode"].permission_risk == "permission_change"
    assert by_name["/harness"].permission_risk == "tool_execution"
    assert by_name["/goal"].source == "shared_runtime"
    assert by_name["/agents"].source == "new_ui"
    assert by_name["/write"].arguments.required is True
    assert by_name["/models"].arguments.required is False
    assert by_name["/doctor"].arguments.takes_arguments is False


def test_tui_index_uses_same_runtime_metadata_with_only_real_local_commands() -> None:
    new_ui = {item.command: item for item in build_terminal_command_index("new_ui")}
    tui = {item.command: item for item in build_terminal_command_index("tui")}

    for command in ("/help", "/harness", "/goal", "/write"):
        assert tui[command] == new_ui[command]
    assert set(tui) - {item.name for item in COMMANDS_META} == {
        "/agents",
        "/cancel-queued",
        "/send-now",
        "/workbench",
    }
    assert "/fold" not in tui

    candidates = InputBar()._build_slash_candidates("work")  # noqa: SLF001
    assert candidates == ["/workbench", "/worktree"]


def test_command_index_models_reject_false_safety_metadata() -> None:
    with pytest.raises(ValidationError, match="readonly"):
        TerminalCommandIndexEntry(
            command="/unsafe",
            aliases=(),
            description="错误标记",
            category="control",
            source="new_ui",
            readonly=True,
            permission_risk="tool_execution",
            arguments=CommandArgumentSchema(
                takes_arguments=False,
                syntax="",
                required=False,
            ),
        )

    with pytest.raises(ValidationError, match="syntax"):
        CommandArgumentSchema(
            takes_arguments=False,
            syntax="<path>",
            required=False,
        )


def test_command_index_rejects_unknown_surface() -> None:
    with pytest.raises(ValueError, match="surface"):
        build_terminal_command_index("legacy")  # type: ignore[arg-type]
