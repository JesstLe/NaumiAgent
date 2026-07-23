"""UI-14.2g authoritative terminal page index tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from naumi_agent.ui.page_index import (
    TerminalPageIndexEntry,
    build_terminal_page_index,
    search_terminal_pages,
    terminal_page_template,
)


def test_page_index_is_deterministic_surface_aware_and_command_backed() -> None:
    new_ui = build_terminal_page_index("new_ui")
    tui = build_terminal_page_index("tui")

    assert new_ui == build_terminal_page_index("new_ui")
    assert [entry.order for entry in new_ui] == sorted(entry.order for entry in new_ui)
    assert len({entry.page_id for entry in new_ui}) == len(new_ui)
    assert len({entry.command for entry in new_ui}) == len(new_ui)
    assert all(entry.schema_version == 1 for entry in new_ui)
    assert all(entry.surface == "new_ui" for entry in new_ui)
    assert all(entry.surface == "tui" for entry in tui)

    new_ui_by_id = {entry.page_id: entry for entry in new_ui}
    tui_by_id = {entry.page_id: entry for entry in tui}
    assert new_ui_by_id["conversation"].command == "/chat"
    assert "conversation" not in tui_by_id
    for page_id in (
        "tasks",
        "goals",
        "agents",
        "workbench",
        "permissions",
        "doctor",
        "evolution",
    ):
        assert tui_by_id[page_id].command == new_ui_by_id[page_id].command


def test_page_search_ranks_exact_localized_metadata_and_fuzzy() -> None:
    entries = build_terminal_page_index("new_ui")

    assert search_terminal_pages(entries, "/doctor")[0].page_id == "doctor"
    assert search_terminal_pages(entries, "目标")[0].page_id == "goals"
    assert search_terminal_pages(entries, "团队消息")[0].page_id == "agents"
    assert search_terminal_pages(entries, "prmssn")[0].page_id == "permissions"
    assert terminal_page_template(search_terminal_pages(entries, "工作树")[0]) == "/workbench"


def test_page_search_is_bounded_and_rejects_invalid_limits() -> None:
    entries = build_terminal_page_index("new_ui")

    assert len(search_terminal_pages(entries, "", limit=3)) == 3
    with pytest.raises(ValueError, match="limit"):
        search_terminal_pages(entries, "", limit=0)
    with pytest.raises(ValueError, match="limit"):
        search_terminal_pages(entries, "", limit=33)
    with pytest.raises(ValueError, match="surface"):
        build_terminal_page_index("legacy")  # type: ignore[arg-type]


def test_page_schema_rejects_unordered_or_unsafe_metadata() -> None:
    with pytest.raises(ValidationError, match="keywords"):
        TerminalPageIndexEntry(
            page_id="unsafe",
            command="/doctor",
            label="诊断",
            description="查看诊断。",
            keywords=("诊断", "doctor"),
            order=1,
            surface="new_ui",
        )
    with pytest.raises(ValidationError, match="控制字符"):
        TerminalPageIndexEntry(
            page_id="unsafe",
            command="/doctor",
            label="诊断\n页面",
            description="查看诊断。",
            keywords=("doctor",),
            order=1,
            surface="new_ui",
        )
