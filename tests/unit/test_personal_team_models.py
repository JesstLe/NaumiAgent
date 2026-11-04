"""Real catalog snapshots and strict team plan/remaining-budget contracts."""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from itertools import count
from types import SimpleNamespace

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.service import PersonalAgentService
from naumi_agent.personal.store import PersonalAgentStore
from naumi_agent.personal.team_models import (
    capture_roster,
    normalize_client_payload,
    normalize_plan,
    phase_budget,
    validate_current,
)
from naumi_agent.tools.base import Tool, ToolMetadata, ToolRegistry


class Capability(Tool):
    def __init__(self, name: str, *, read_only: bool = False):
        self._name = name
        self._read_only = read_only

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return "已注册的成员测试能力"

    @property
    def parameters_schema(self):
        return {"type": "object", "additionalProperties": False}

    @property
    def metadata(self):
        return ToolMetadata(read_only=self._read_only)

    async def execute(self, **kwargs):
        return "真实工具调用由 Engine 集成测试验证"


@pytest.fixture
def catalog(tmp_path):
    registry = ToolRegistry()
    for name in ("file_read", "web_fetch"):
        registry.register(Capability(name, read_only=True))
    for name in ("file_write", "bash_run", "personal_message", "request_user_input"):
        registry.register(Capability(name))
    engine = SimpleNamespace(
        workspace_root=tmp_path,
        config=SimpleNamespace(memory=MemoryConfig(session_db_path=str(tmp_path / "session.db"))),
        tool_registry=registry,
    )
    store = PersonalAgentStore(tmp_path / "personal")
    personal = PersonalAgentService(engine, store)
    ids = count()

    def create(**fields):
        number = next(ids)
        return personal.create(
            **{
                "name": f"用户明确创建的角色{number}",
                "instructions": "只处理本次明确共享的资料",
                "request_id": f"catalog-{number}",
                **fields,
            }
        )

    return personal, registry, create


@contextmanager
def error(code="invalid_input"):
    with pytest.raises(PersonalAgentError) as caught:
        yield
    assert caught.value.code == code
    assert any("\u4e00" <= char <= "\u9fff" for char in caught.value.message)


def request(**changes):
    return {
        "team_id": "pa_" + "1" * 32,
        "team_version": 1,
        "request_id": "user-team-turn-1",
        "content": "读取指定资料，核算后由团队汇总。",
        **changes,
    }


def step(agent, identity="read", **changes):
    return {
        "id": identity,
        "agent_id": agent.id,
        "agent_version": agent.version,
        "task": "读取显式资料并给出来源",
        "depends_on": [],
        **changes,
    }


def policy(**changes):
    return {"max_budget_usd": 10.0, "max_input_tokens": 100, "max_output_tokens": 50, **changes}


def spent(**changes):
    return {"total_cost_usd": 2.1, "input_tokens": 9, "output_tokens": 7, **changes}


def test_client_defaults_are_json_stable_and_do_not_mutate_the_original():
    original = request(source_ids=["src-z", "src-a"], member_grants=None)
    before = copy.deepcopy(original)
    result = normalize_client_payload(original)
    assert result == {**request(), "context": "", "source_ids": ["src-a", "src-z"]}
    assert original == before
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result
    assert normalize_client_payload(result) == result
    assert "member_grants" not in result
    assert normalize_client_payload(request(member_grants=[]))["member_grants"] == []


def test_client_normalizes_grants_but_keeps_explicit_empty_tools_distinct():
    identity = "pa_" + "2" * 32
    value = request(member_grants=[{"agent_id": identity, "agent_version": 2, "tool_names": []}])
    result = normalize_client_payload(value)
    assert result["member_grants"] == value["member_grants"]
    result["member_grants"][0]["tool_names"].append("file_write")
    assert value["member_grants"][0]["tool_names"] == []
    omitted = normalize_client_payload(
        request(member_grants=[{"agent_id": identity, "agent_version": 2}])
    )
    assert "tool_names" not in omitted["member_grants"][0]


@pytest.mark.parametrize("value", [None, [], "request", True, 1])
def test_client_requires_an_object(value):
    with error():
        normalize_client_payload(value)


@pytest.mark.parametrize("missing", ["team_id", "team_version", "request_id", "content"])
def test_client_rejects_missing_fields(missing):
    value = request()
    value.pop(missing)
    with error():
        normalize_client_payload(value)


@pytest.mark.parametrize(
    "fields",
    [
        {"source_session_id": "forged-source"},
        {"team_id": "other"},
        {"team_version": True},
        {"team_version": "1"},
        {"team_version": 0},
        {"team_version": 2_147_483_648},
        {"request_id": " leading-space"},
        {"request_id": "x" * 129},
        {"request_id": "\ud800"},
        {"content": " \n\t"},
        {"content": "x" * 16001},
        {"content": "x\x00"},
        {"content": "x\x7f"},
        {"content": "x\x80"},
        {"content": "\ud800"},
        {"context": None},
        {"context": "x" * 32001},
        {"context": "\udfff"},
        {"source_ids": "src-1"},
        {"source_ids": ("src-1",)},
        {"source_ids": ["src"] * 2},
        {"source_ids": [f"src-{n}" for n in range(33)]},
        {"source_ids": [""]},
        {"source_ids": ["x" * 129]},
        {"source_ids": ["src\t1"]},
        {"source_ids": ["src\x00"]},
        {"source_ids": ["\ud800"]},
        {"member_grants": {}},
        {"member_grants": [{"agent_id": "pa_" + "2" * 32, "agent_version": True}]},
    ],
)
def test_client_rejects_malformed_untrusted_values(fields):
    with error():
        normalize_client_payload(request(**fields))


def test_client_accepts_exact_text_boundaries_and_preserves_body_whitespace():
    result = normalize_client_payload(
        request(
            content="文" * 16000, context="料" * 32000, source_ids=[f"src-{n}" for n in range(32)]
        )
    )
    assert len(result["content"]) == 16000 and len(result["context"]) == 32000
    assert len(result["source_ids"]) == 32
    assert (
        normalize_client_payload(request(content="  一\n二\t三  "))["content"] == "  一\n二\t三  "
    )


def test_nested_roster_is_deduplicated_and_never_inherits_coordinator_or_write_tools(catalog):
    personal, registry, create = catalog
    reader = create(tool_names=["file_read", "file_write", "personal_message"])
    browser = create(tool_names=["web_fetch", "bash_run"])
    nested = create(kind="team", member_ids=[reader.id, browser.id])
    team = create(kind="team", tool_names=["file_write"], member_ids=[nested.id, reader.id])
    before = personal.list()
    roster = capture_roster(personal, team, registry)
    assert set(roster) == {"teams", "agents"}
    assert {t["agent_id"] for t in roster["teams"]} == {team.id, nested.id}
    assert len(roster["agents"]) == 2
    scopes = {entry["agent_id"]: entry["tool_names"] for entry in roster["agents"]}
    assert scopes == {reader.id: ["file_read", "personal_message"], browser.id: ["web_fetch"]}
    assert all(set(row) == {"agent_id", "agent_version", "member_ids"} for row in roster["teams"])
    assert all(set(row) == {"agent_id", "agent_version", "tool_names"} for row in roster["agents"])
    assert personal.list() == before
    assert json.loads(json.dumps(roster)) == roster


def test_explicit_grants_select_only_the_named_leaf_and_allow_its_actual_write_scope(catalog):
    personal, registry, create = catalog
    writer = create(tool_names=["file_read", "file_write"])
    other = create(tool_names=["file_read"])
    team = create(kind="team", member_ids=[writer.id, other.id])
    original = [{"agent_id": writer.id, "agent_version": 1, "tool_names": ["file_write"]}]
    before = copy.deepcopy(original)
    roster = capture_roster(personal, team, registry, original)
    assert roster["agents"] == [
        {"agent_id": writer.id, "agent_version": 1, "tool_names": ["file_write"]}
    ]
    assert original == before
    assert capture_roster(personal, team, registry, [{"agent_id": writer.id, "agent_version": 1}])[
        "agents"
    ][0]["tool_names"] == ["file_read"]


def test_explicit_empty_grants_preserve_ancestors_without_authorizing_any_leaf(catalog):
    personal, registry, create = catalog
    member = create(tool_names=["file_write"])
    nested = create(kind="team", member_ids=[member.id])
    team = create(kind="team", member_ids=[nested.id])
    roster = capture_roster(personal, team, registry, [])
    assert roster["agents"] == [] and len(roster["teams"]) == 2
    personal.update(member.id, member.version, status="paused")
    validate_current(personal, roster)
    assert capture_roster(personal, team, registry, []) == roster


@pytest.mark.parametrize("status", ["paused", "archived"])
@pytest.mark.parametrize("selection", ["empty", "subset"])
def test_scope_c_unselected_inactive_leaf_does_not_block_intake(catalog, status, selection):
    personal, registry, create = catalog
    selected = create(tool_names=["file_read"])
    excluded = create(tool_names=["file_write"])
    nested = create(kind="team", member_ids=[selected.id, excluded.id])
    team = create(kind="team", member_ids=[nested.id, excluded.id])
    personal.update(excluded.id, excluded.version, status=status, instructions="未授权的新职责")
    grants = (
        []
        if selection == "empty"
        else [{"agent_id": selected.id, "agent_version": selected.version, "tool_names": []}]
    )
    roster = capture_roster(personal, team, registry, grants)
    assert {entry["agent_id"] for entry in roster["teams"]} == {team.id, nested.id}
    assert roster["agents"] == grants
    validate_current(personal, roster)
    with error("agent_inactive"):
        capture_roster(personal, team, registry)


@pytest.mark.parametrize("status", ["paused", "archived"])
@pytest.mark.parametrize("selection", ["default", "explicit"])
def test_scope_c_selected_inactive_leaf_is_rejected(catalog, status, selection):
    personal, registry, create = catalog
    member = create(tool_names=["file_read"])
    team = create(kind="team", member_ids=[member.id])
    member = personal.update(member.id, member.version, status=status)
    grants = (
        None
        if selection == "default"
        else [{"agent_id": member.id, "agent_version": member.version, "tool_names": []}]
    )
    with error("agent_inactive"):
        capture_roster(personal, team, registry, grants)


@pytest.mark.parametrize("status", ["paused", "archived"])
@pytest.mark.parametrize("node", ["root", "nested"])
@pytest.mark.parametrize("selection", ["empty", "subset"])
def test_scope_c_inactive_team_is_rejected_even_without_member_grants(
    catalog, status, node, selection
):
    personal, registry, create = catalog
    member = create(tool_names=["file_read"])
    nested = create(kind="team", member_ids=[member.id])
    team = create(kind="team", member_ids=[nested.id])
    target = team if node == "root" else nested
    personal.update(target.id, target.version, status=status)
    grants = (
        []
        if selection == "empty"
        else [{"agent_id": member.id, "agent_version": member.version}]
    )
    with error("agent_inactive"):
        capture_roster(personal, team, registry, grants)


def test_scope_c_missing_unselected_leaf_still_blocks_intake(catalog):
    personal, registry, create = catalog
    team = create(kind="team")
    with sqlite3.connect(personal.store.db_path) as connection:
        connection.execute(
            "UPDATE personal_agents SET member_ids=? WHERE id=?",
            (json.dumps(["pa_" + "f" * 32]), team.id),
        )
    with error("not_found"):
        capture_roster(personal, personal.get(team.id), registry, [])


def test_scope_c_capture_permits_unselected_leaf_pause_during_snapshot(catalog, monkeypatch):
    personal, registry, create = catalog
    member = create(tool_names=["file_write"])
    team = create(kind="team", member_ids=[member.id])
    get = personal.get
    reads = 0

    def changed(identity):
        nonlocal reads
        if identity == team.id:
            reads += 1
            if reads == 2:
                personal.update(member.id, member.version, status="paused")
        return get(identity)

    monkeypatch.setattr(personal, "get", changed)
    roster = capture_roster(personal, team, registry, [])
    assert roster["agents"] == []
    assert get(member.id).status == "paused"
    validate_current(personal, roster)


def test_live_validation_does_not_treat_omitted_nested_team_as_an_ungranted_leaf(catalog):
    personal, registry, create = catalog
    member = create()
    nested = create(kind="team", member_ids=[member.id])
    team = create(kind="team", member_ids=[nested.id])
    roster = capture_roster(personal, team, registry, [])
    roster["teams"] = [entry for entry in roster["teams"] if entry["agent_id"] != nested.id]
    assert roster["agents"] == []
    with error("team_scope_denied"):
        validate_current(personal, roster)
    personal.update(nested.id, nested.version, member_ids=[])
    with error("team_scope_denied"):
        validate_current(personal, roster)


def test_subset_validation_resolves_ungranted_leaf_kind_without_binding_its_revision(
    catalog, monkeypatch
):
    personal, registry, create = catalog
    selected = create(tool_names=["file_read"])
    ungranted = create(tool_names=["file_write"])
    nested = create(kind="team", member_ids=[selected.id, ungranted.id])
    team = create(kind="team", member_ids=[nested.id, ungranted.id])
    roster = capture_roster(
        personal,
        team,
        registry,
        [{"agent_id": selected.id, "agent_version": selected.version, "tool_names": []}],
    )
    assert [entry["agent_id"] for entry in roster["agents"]] == [selected.id]
    personal.update(
        ungranted.id, ungranted.version, instructions="未授权专员的新职责", status="paused"
    )
    get = personal.get
    looked_up = []

    def tracked_get(identity):
        looked_up.append(identity)
        return get(identity)

    monkeypatch.setattr(personal, "get", tracked_get)
    validate_current(personal, roster)
    assert looked_up.count(ungranted.id) == 1
    with sqlite3.connect(personal.store.db_path) as connection:
        connection.execute("UPDATE personal_agents SET kind='team' WHERE id=?", (ungranted.id,))
    with error("team_scope_denied"):
        validate_current(personal, roster)


@pytest.mark.parametrize("target", ["foreign", "nested", "version", "tool"])
def test_roster_rejects_forged_recipient_version_or_tools(catalog, target):
    personal, registry, create = catalog
    member = create(tool_names=["file_read"])
    foreign = create(tool_names=["file_read"])
    nested = create(kind="team", member_ids=[member.id])
    team = create(kind="team", member_ids=[nested.id])
    grant = {"agent_id": member.id, "agent_version": 1}
    if target == "foreign":
        grant["agent_id"] = foreign.id
    elif target == "nested":
        grant["agent_id"] = nested.id
    elif target == "version":
        grant["agent_version"] = 2
    else:
        grant["tool_names"] = ["bash_run"]
    with error("version_conflict" if target == "version" else "team_scope_denied"):
        capture_roster(personal, team, registry, [grant])


def test_roster_default_ignores_nonboolean_read_only_metadata(catalog):
    personal, registry, create = catalog
    registry.register(Capability("file_read", read_only="unclassified"))
    member = create(tool_names=["file_read"])
    team = create(kind="team", member_ids=[member.id])
    assert capture_roster(personal, team, registry)["agents"][0]["tool_names"] == []


def test_unregistered_tool_is_not_default_granted_and_explicit_scope_fails_closed(catalog):
    personal, registry, create = catalog
    member = create(tool_names=["file_read"])
    team = create(kind="team", member_ids=[member.id])
    assert registry.unregister_if_same("file_read", registry.get("file_read"))
    assert capture_roster(personal, team, registry)["agents"][0]["tool_names"] == []
    with error("tool_not_assignable"):
        capture_roster(
            personal,
            team,
            registry,
            [{"agent_id": member.id, "agent_version": 1, "tool_names": ["file_read"]}],
        )


def test_leaf_never_receives_the_team_coordinator_capability_even_with_a_forged_profile(catalog):
    personal, registry, create = catalog
    member = create()
    team = create(kind="team", member_ids=[member.id])
    with sqlite3.connect(personal.store.db_path) as connection:
        connection.execute(
            "UPDATE personal_agents SET tool_names=? WHERE id=?",
            (json.dumps(["personal_team"]), member.id),
        )
    registry.register(Capability("personal_team"))
    with error("team_scope_denied"):
        capture_roster(
            personal,
            team,
            registry,
            [{"agent_id": member.id, "agent_version": 1, "tool_names": ["personal_team"]}],
        )


@pytest.mark.parametrize("node", ["top", "nested", "member"])
def test_capture_rejects_inactive_nodes(catalog, node):
    personal, registry, create = catalog
    member = create()
    nested = create(kind="team", member_ids=[member.id])
    team = create(kind="team", member_ids=[nested.id])
    selected = {"top": team, "nested": nested, "member": member}[node]
    personal.update(selected.id, selected.version, status="paused")
    with error("agent_inactive"):
        capture_roster(personal, team, registry)


def test_capture_rejects_stale_or_forged_team_and_specialist_identity(catalog):
    personal, registry, create = catalog
    member = create()
    team = create(kind="team", member_ids=[member.id])
    with error():
        capture_roster(personal, member, registry)
    with error():
        capture_roster(personal, None, registry)
    with error("binding_conflict"):
        capture_roster(personal, replace(team, member_ids=()), registry)
    personal.update(team.id, 1, instructions="新职责")
    with error("version_conflict"):
        capture_roster(personal, team, registry)


@pytest.mark.parametrize("change", ["instructions", "remove", "pause"])
def test_nested_team_changes_fence_old_roster_even_when_top_version_is_unchanged(catalog, change):
    personal, registry, create = catalog
    member = create(tool_names=["file_read"])
    nested = create(kind="team", member_ids=[member.id])
    team = create(kind="team", member_ids=[nested.id])
    roster = capture_roster(personal, team, registry)
    fields = {
        "instructions": {"instructions": "新职责"},
        "remove": {"member_ids": []},
        "pause": {"status": "paused"},
    }[change]
    personal.update(nested.id, nested.version, **fields)
    assert personal.get(team.id).version == team.version
    with error("agent_inactive" if change == "pause" else "version_conflict"):
        validate_current(personal, roster)


@pytest.mark.parametrize("fields", [{"instructions": "新职责"}, {"status": "paused"}])
def test_authorized_leaf_revisions_or_pause_fence_old_snapshot(catalog, fields):
    personal, registry, create = catalog
    member = create(tool_names=["file_read"])
    team = create(kind="team", member_ids=[member.id])
    roster = capture_roster(personal, team, registry)
    personal.update(member.id, member.version, **fields)
    with error("agent_inactive" if "status" in fields else "version_conflict"):
        validate_current(personal, roster)


def test_tool_revocation_without_version_change_is_still_rejected(catalog):
    personal, registry, create = catalog
    member = create(tool_names=["file_read"])
    team = create(kind="team", member_ids=[member.id])
    roster = capture_roster(personal, team, registry)
    with sqlite3.connect(personal.store.db_path) as connection:
        connection.execute("UPDATE personal_agents SET tool_names='[]' WHERE id=?", (member.id,))
    with error("team_scope_denied"):
        validate_current(personal, roster)


def test_missing_catalog_fails_closed_without_creating_a_new_database(catalog):
    personal, registry, create = catalog
    team = create(kind="team")
    roster = capture_roster(personal, team, registry)
    database = personal.store.db_path
    database.rename(database.with_suffix(".lost"))
    with error("catalog_unavailable"):
        validate_current(personal, roster)
    with error("catalog_unavailable"):
        capture_roster(personal, team, registry)
    assert not database.exists()


def test_capture_rechecks_versions_changed_during_flattening(catalog, monkeypatch):
    personal, registry, create = catalog
    member = create(tool_names=["file_read"])
    team = create(kind="team", member_ids=[member.id])
    get = personal.get
    calls = 0

    def changing(identity):
        nonlocal calls
        if identity == team.id:
            calls += 1
            if calls == 2:
                personal.update(member.id, member.version, instructions="并发更新后的职责")
        return get(identity)

    monkeypatch.setattr(personal, "get", changing)
    with error("version_conflict"):
        capture_roster(personal, team, registry)
    assert get(member.id).version == 2


def test_corrupt_catalog_cycle_or_missing_member_does_not_become_a_leaf(catalog):
    personal, registry, create = catalog
    team = create(kind="team")
    with sqlite3.connect(personal.store.db_path) as connection:
        connection.execute(
            "UPDATE personal_agents SET member_ids=? WHERE id=?", (json.dumps([team.id]), team.id)
        )
    with error("member_cycle"):
        capture_roster(personal, personal.get(team.id), registry)
    missing = "pa_" + "f" * 32
    with sqlite3.connect(personal.store.db_path) as connection:
        connection.execute(
            "UPDATE personal_agents SET member_ids=? WHERE id=?", (json.dumps([missing]), team.id)
        )
    with error("not_found"):
        capture_roster(personal, personal.get(team.id), registry)


def test_exact_eight_edge_depth_is_supported_and_ninth_is_rejected(catalog):
    personal, registry, create = catalog
    member = create()
    team = member
    for _ in range(8):
        team = create(kind="team", member_ids=[team.id])
    assert len(capture_roster(personal, team, registry)["teams"]) == 8
    too_deep = create(kind="team", member_ids=[team.id])
    with error("team_depth"):
        capture_roster(personal, too_deep, registry)


def test_shared_team_first_reached_shallow_still_counts_on_the_longest_path(catalog):
    personal, registry, create = catalog
    member = create()
    shared = create(kind="team", member_ids=[member.id])
    chain = shared
    for _ in range(7):
        chain = create(kind="team", member_ids=[chain.id])
    team = create(kind="team", member_ids=[shared.id, chain.id])
    with error("team_depth"):
        capture_roster(personal, team, registry)


def test_flattened_leaf_limit_is_not_silently_truncated(catalog):
    personal, registry, create = catalog
    members = [create() for _ in range(129)]
    one = create(kind="team", member_ids=[m.id for m in members[:64]])
    two = create(kind="team", member_ids=[m.id for m in members[64:]])
    team = create(kind="team", member_ids=[one.id, two.id])
    with error("team_limit"):
        capture_roster(personal, team, registry)


def test_flattened_team_limit_counts_the_root_as_well(catalog):
    personal, registry, create = catalog
    children = [create(kind="team") for _ in range(128)]
    team = create(kind="team", member_ids=[m.id for m in children])
    with error("team_limit"):
        capture_roster(personal, team, registry)


def test_valid_plan_uses_real_leaf_version_and_reuses_existing_dag_rules(catalog):
    personal, registry, create = catalog
    member = create()
    other = create()
    team = create(kind="team", member_ids=[member.id, other.id])
    roster = capture_roster(personal, team, registry)
    value = [
        step(member, "first"),
        step(other, "second"),
        step(member, "review", depends_on=["second", "first"]),
    ]
    before = copy.deepcopy(value)
    result = normalize_plan(value, roster)
    assert result[-1]["depends_on"] == ["first", "second"]
    assert value == before
    assert all(
        set(item) == {"id", "agent_id", "agent_version", "task", "depends_on"} for item in result
    )


@pytest.mark.parametrize(
    "problem",
    [
        "empty",
        "too_many",
        "duplicate",
        "unknown",
        "cycle",
        "self",
        "bad_id",
        "bool_version",
        "extra",
        "missing",
        "task",
    ],
)
def test_plan_rejects_invalid_dag_or_internal_fields(catalog, problem):
    personal, registry, create = catalog
    member = create()
    team = create(kind="team", member_ids=[member.id])
    roster = capture_roster(personal, team, registry)
    value = [step(member)]
    if problem == "empty":
        value = []
    elif problem == "too_many":
        value = [step(member, f"part{n}") for n in range(13)]
    elif problem == "duplicate":
        value *= 2
    elif problem == "unknown":
        value[0]["depends_on"] = ["unknown"]
    elif problem == "cycle":
        value = [step(member, "a", depends_on=["b"]), step(member, "b", depends_on=["a"])]
    elif problem == "self":
        value[0]["depends_on"] = ["read"]
    elif problem == "bad_id":
        value[0]["id"] = "no whitespace"
    elif problem == "bool_version":
        value[0]["agent_version"] = True
    elif problem == "extra":
        value[0]["message"] = {"forged": "dispatch"}
    elif problem == "missing":
        value[0].pop("task")
    else:
        value[0]["task"] = "\x00"
    with error():
        normalize_plan(value, roster)


def test_plan_never_assigns_foreign_ungranted_team_or_new_version(catalog):
    personal, registry, create = catalog
    member = create()
    foreign = create()
    team = create(kind="team", member_ids=[member.id])
    roster = capture_roster(personal, team, registry)
    for candidate in (foreign, team):
        with error("team_scope_denied"):
            normalize_plan([step(candidate)], roster)
    with error("version_conflict"):
        normalize_plan([step(member, agent_version=2)], roster)
    with error("team_scope_denied"):
        normalize_plan([step(member)], capture_roster(personal, team, registry, []))


@pytest.mark.parametrize("change", ["extra", "duplicate", "foreign", "missing_root", "bad_version"])
def test_plan_and_live_validation_reject_malformed_roster(catalog, change):
    personal, registry, create = catalog
    member = create()
    team = create(kind="team", member_ids=[member.id])
    roster = capture_roster(personal, team, registry)
    if change == "extra":
        roster["grants"] = []
    elif change == "duplicate":
        roster["agents"] *= 2
    elif change == "foreign":
        roster["agents"][0]["agent_id"] = create().id
    elif change == "missing_root":
        roster["teams"] = []
    else:
        roster["agents"][0]["agent_version"] = False
    code = "team_scope_denied" if change == "foreign" else "invalid_input"
    with error(code):
        normalize_plan([step(member)], roster)
    with error(code):
        validate_current(personal, roster)


def test_remaining_budget_is_split_without_minting_a_fresh_limit():
    original, used = policy(), spent()
    before = copy.deepcopy((original, used))
    result = phase_budget(original, used, 3)
    assert result == {"max_budget_usd": 2.633333, "max_input_tokens": 30, "max_output_tokens": 14}
    assert (original, used) == before
    assert Decimal(str(result["max_budget_usd"])) * 3 <= Decimal("7.9")


@pytest.mark.parametrize(
    ("limit", "used", "slots", "expected"),
    [
        (0.3, 0.1, 3, 0.066666),
        (0.000003, 0.0000001, 2, 0.000001),
        (0.0000009, 0, 1, 0),
        (0.1, 0.2, 1, 0),
        (0, 0, 5, 0),
        (1_000_000_000, 0, 1, 1_000_000_000.0),
    ],
)
def test_microdollar_rounding_is_downward_and_never_overallocates(limit, used, slots, expected):
    result = phase_budget(policy(max_budget_usd=limit), spent(total_cost_usd=used), slots)
    assert result["max_budget_usd"] == expected
    assert Decimal(str(result["max_budget_usd"])) * slots <= max(
        Decimal(0), Decimal(str(limit)) - Decimal(str(used))
    )


def test_summary_uses_actual_unspent_budget_and_zero_stays_zero():
    original = policy(max_budget_usd=0.3, max_input_tokens=300, max_output_tokens=90)
    members = phase_budget(original, spent(total_cost_usd=0.1, input_tokens=30, output_tokens=9), 3)
    assert members == {"max_budget_usd": 0.066666, "max_input_tokens": 90, "max_output_tokens": 27}
    summary = phase_budget(original, spent(total_cost_usd=0.22, input_tokens=100, output_tokens=50))
    assert summary == {"max_budget_usd": 0.08, "max_input_tokens": 200, "max_output_tokens": 40}
    assert phase_budget(
        policy(max_budget_usd=0, max_input_tokens=0, max_output_tokens=0),
        spent(total_cost_usd=0, input_tokens=0, output_tokens=0),
    ) == {"max_budget_usd": 0.0, "max_input_tokens": 0, "max_output_tokens": 0}


def test_unbounded_fields_remain_unbounded_but_other_limits_and_spend_are_checked():
    result = phase_budget(
        policy(max_budget_usd=None, max_input_tokens=None), spent(output_tokens=99)
    )
    assert result == {"max_budget_usd": None, "max_input_tokens": None, "max_output_tokens": 0}
    result = phase_budget(
        policy(max_budget_usd=None, max_input_tokens=None, max_output_tokens=None),
        spent(total_cost_usd=9999, input_tokens=9999, output_tokens=9999),
        7,
    )
    assert result == {"max_budget_usd": None, "max_input_tokens": None, "max_output_tokens": None}


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "2", None])
def test_budget_slots_must_be_positive_integer(value):
    with error():
        phase_budget(policy(), spent(), value)


@pytest.mark.parametrize("field", ["max_budget_usd", "max_input_tokens", "max_output_tokens"])
@pytest.mark.parametrize("value", [True, -1, "1", float("inf"), float("nan")])
def test_budget_policy_limits_reject_invalid_numbers(field, value):
    with error():
        phase_budget(policy(**{field: value}), spent())


@pytest.mark.parametrize("field", ["total_cost_usd", "input_tokens", "output_tokens"])
@pytest.mark.parametrize("value", [True, -1, "1", None, float("inf"), float("nan")])
def test_budget_spend_requires_exact_nonnegative_counters(field, value):
    with error():
        phase_budget(policy(), spent(**{field: value}))


@pytest.mark.parametrize("field", ["max_input_tokens", "max_output_tokens"])
def test_token_limit_rejects_a_float_even_when_integral(field):
    with error():
        phase_budget(policy(**{field: 1.0}), spent())


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
def test_token_spend_rejects_a_float_even_when_integral(field):
    with error():
        phase_budget(policy(), spent(**{field: 1.0}))


@pytest.mark.parametrize(
    "extra",
    [
        {"max_turns": False},
        {"max_turns": 0},
        {"max_turns": 1001},
        {"permission_mode": "unknown"},
        {"permission_mode": []},
        {"unexpected": None},
    ],
)
def test_budget_rejects_invalid_policy_envelope(extra):
    with error():
        phase_budget(policy(**extra), spent())


def test_budget_rejects_missing_fields_unknown_spend_or_worker_contract_overflow():
    incomplete = policy()
    incomplete.pop("max_input_tokens")
    for value in (None, [], incomplete):
        with error():
            phase_budget(value, spent())
    for value in (None, [], {**spent(), "other": 0}):
        with error():
            phase_budget(policy(), value)
    with error():
        phase_budget(policy(max_budget_usd=1_000_000_001), spent())
    with error():
        phase_budget(policy(max_budget_usd=Decimal("1")), spent())
    assert (
        phase_budget(policy(max_turns=1000, permission_mode="lockdown"), spent())["max_budget_usd"]
        == 7.9
    )
