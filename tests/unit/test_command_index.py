"""UI-14.1 authoritative terminal command index tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from naumi_agent.cli.completer import COMMANDS_META
from naumi_agent.tui.app import InputBar
from naumi_agent.ui.command_index import (
    CommandArgumentSchema,
    TerminalCommandIndexEntry,
    build_terminal_command_index,
    record_recent_terminal_command,
    search_terminal_commands,
    terminal_command_template,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RECENCY_GOLDEN = json.loads(
    (
        PROJECT_ROOT
        / "tests"
        / "fixtures"
        / "ui14"
        / "command-recency-golden.json"
    ).read_text(encoding="utf-8")
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
    assert by_name["/agents"].arguments.syntax == "[agent <name>]"
    assert by_name["/agents"].arguments.required is False
    assert by_name["/write"].arguments.required is True
    assert by_name["/models"].arguments.required is False
    assert by_name["/doctor"].arguments.syntax == "[export [snapshot-sha256]]"
    assert by_name["/doctor"].arguments.required is False
    assert by_name["/doctor"].permission_risk == "tool_execution"


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


def test_command_search_ranks_alias_fuzzy_and_localized_risk() -> None:
    entries = build_terminal_command_index("new_ui")

    assert search_terminal_commands(entries, "/h", limit=5)[0].command == "/help"
    assert search_terminal_commands(entries, "wr", limit=5)[0].command == "/write"
    write_results = search_terminal_commands(entries, "工作区写入", limit=50)
    assert write_results
    assert all(item.permission_risk == "workspace_write" for item in write_results)

    write = next(item for item in entries if item.command == "/write")
    assert terminal_command_template(write) == f"/write {write.arguments.syntax}"
    help_entry = next(item for item in entries if item.command == "/help")
    assert terminal_command_template(help_entry) == "/help"
    tasks = next(item for item in entries if item.command == "/tasks")
    assert tasks.arguments.syntax == "[detail <id>]"
    assert tasks.readonly is True


def test_command_search_is_bounded_and_rejects_invalid_limits() -> None:
    entries = build_terminal_command_index("new_ui")

    assert len(search_terminal_commands(entries, "", limit=3)) == 3
    with pytest.raises(ValueError, match="limit"):
        search_terminal_commands(entries, "", limit=0)


def test_recent_commands_match_shared_golden_without_storing_arguments() -> None:
    entries = build_terminal_command_index("new_ui")
    recent: tuple[str, ...] = ()
    for submission in RECENCY_GOLDEN["submissions"]:
        recent = record_recent_terminal_command(entries, recent, submission)

    assert list(recent) == RECENCY_GOLDEN["expected_recent_commands"]
    assert "private" not in json.dumps(recent)
    empty_results = search_terminal_commands(
        entries,
        "",
        limit=10,
        recent_commands=recent,
    )
    assert [item.command for item in empty_results[:2]] == (
        RECENCY_GOLDEN["empty_query_order_prefix"]
    )
    query_results = search_terminal_commands(
        entries,
        RECENCY_GOLDEN["query"],
        limit=10,
        recent_commands=recent,
    )
    assert [item.command for item in query_results[:1]] == (
        RECENCY_GOLDEN["query_order_prefix"]
    )


def test_recent_commands_are_deduplicated_bounded_and_validate_limit() -> None:
    entries = build_terminal_command_index("new_ui")
    recent = tuple(item.command for item in entries[:20])
    updated = record_recent_terminal_command(entries, recent, recent[-1], limit=20)

    assert updated[0] == recent[-1]
    assert len(updated) == len(set(updated)) == 20
    with pytest.raises(ValueError, match="limit"):
        record_recent_terminal_command(entries, (), "/help", limit=21)
