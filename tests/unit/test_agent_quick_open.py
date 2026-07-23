"""UI-14.2f Agent QuickOpen ranking and safe deep-link tests."""

from __future__ import annotations

import pytest

from naumi_agent.agent_control import AgentDescriptor
from naumi_agent.ui.agent_quick_open import (
    parse_terminal_agent_deep_link,
    search_terminal_agents,
    terminal_agent_template,
)


def _agent(
    name: str,
    *,
    state: str,
    kind: str = "preset",
    description: str = "",
    capabilities: tuple[str, ...] = (),
) -> AgentDescriptor:
    return AgentDescriptor(
        name=name,
        description=description,
        kind=kind,
        state=state,
        capabilities=capabilities,
    )


def test_agent_quick_open_ranks_authoritative_metadata_and_round_trips() -> None:
    items = (
        _agent("reviewer", state="idle", description="代码审查", capabilities=("review",)),
        _agent("Explore Worker", state="running", kind="dynamic", description="探索项目"),
        _agent("builder", state="ready", description="实现功能"),
    )

    assert [item.name for item in search_terminal_agents(items, "")] == [
        "Explore Worker",
        "builder",
        "reviewer",
    ]
    assert search_terminal_agents(items, "审查")[0].name == "reviewer"
    selected = search_terminal_agents(items, "explore")[0]
    template = terminal_agent_template(selected)
    assert template == "/agents agent 'Explore Worker'"
    assert parse_terminal_agent_deep_link(template) == "Explore Worker"


def test_agent_quick_open_rejects_unsafe_names_and_bounds() -> None:
    unsafe = _agent("bad\nagent", state="running")
    assert search_terminal_agents((unsafe,), "") == ()
    with pytest.raises(ValueError, match="安全"):
        terminal_agent_template(unsafe)
    with pytest.raises(ValueError, match="limit"):
        search_terminal_agents((), "", limit=51)
    assert parse_terminal_agent_deep_link("/agents") is None
    assert parse_terminal_agent_deep_link("/agents execution task-1") is None
    assert parse_terminal_agent_deep_link("/agents agent 'unterminated") is None


def test_agent_quick_open_round_trips_shell_metacharacters_as_plain_name() -> None:
    name = "O'Brien $(touch nope)"
    template = terminal_agent_template(name)

    assert template == "/agents agent 'O'\"'\"'Brien $(touch nope)'"
    assert parse_terminal_agent_deep_link(template) == name
