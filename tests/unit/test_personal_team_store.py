"""Real SQLite immutable team requests, idempotent events and tamper detection."""

from __future__ import annotations

import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.team_store import TeamTurnStore

TEAM = "pa_" + "a" * 32
CHILD = "pa_" + "b" * 32
OTHER = "pa_" + "c" * 32


def payload(**changes):
    value = dict(
        workspace_key="workspace",
        source_session_id="main-session",
        team_session_id="team-session",
        team_id=TEAM,
        team_version=1,
        request_id="turn-request",
        client_payload={"content": "整理显式资料", "context": ""},
        roster={
            "teams": [{"agent_id": TEAM, "agent_version": 1, "member_ids": [CHILD]}],
            "agents": [{"agent_id": CHILD, "agent_version": 2, "tool_names": ["file_read"]}],
        },
        policy={
            "permission_mode": "moderate",
            "max_budget_usd": None,
            "max_turns": 5,
            "max_input_tokens": None,
            "max_output_tokens": 0,
        },
    )
    value.update(changes)
    return value


def test_create_restart_replay_and_detached_records(tmp_path):
    store = TeamTurnStore(tmp_path)
    args = payload()
    original = store.create(**args)
    assert original["id"].startswith("pt_") and len(original["id"]) == 35
    assert original["cancel_requested"] is False
    assert not {"status", "state", "result", "response", "lease"} & original.keys()
    args["client_payload"]["content"] = "caller mutation"
    original["roster"]["agents"].clear()
    reopened = TeamTurnStore(tmp_path)
    actual = reopened.get(original["id"], "workspace")
    assert actual["client_payload"]["content"] == "整理显式资料"
    assert len(actual["roster"]["agents"]) == 1
    assert reopened.create(**payload()) == actual
    assert reopened.find_request("workspace", "main-session", "turn-request") == actual
    assert reopened.find_request("workspace", "different-source", "turn-request") is None


def test_event_chain_order_replay_cancel_and_pagination(tmp_path):
    store = TeamTurnStore(tmp_path)
    turn = store.create(**payload())
    first = store.append_event(
        turn["id"],
        "workspace",
        key="plan",
        kind="plan_committed",
        data={"steps": [{"agent_id": CHILD, "task": "读取指定文件"}]},
    )
    assert (
        store.append_event(
            turn["id"],
            "workspace",
            key="plan",
            kind="plan_committed",
            data={"steps": [{"agent_id": CHILD, "task": "读取指定文件"}]},
        )
        == first
    )
    with pytest.raises(PersonalAgentError) as caught:
        store.append_event(turn["id"], "workspace", key="plan", kind="plan_committed", data={})
    assert caught.value.code == "idempotency_conflict"
    assert store.request_cancel(turn["id"], "workspace")["cancel_requested"] is True
    store.request_cancel(turn["id"], "workspace")
    events = store.events(turn["id"], "workspace")
    assert [x["kind"] for x in events] == ["created", "plan_committed", "cancel_requested"]
    assert [x["sequence"] for x in events] == [1, 2, 3]
    assert events[1]["previous_sha256"] == events[0]["receipt_sha256"]
    assert store.events(turn["id"], "workspace", after=1, limit=1) == [first]
    assert store.events(turn["id"], "workspace", after=10) == []


def test_concurrent_create_and_event_claim_have_one_identity(tmp_path):
    def create(_):
        return TeamTurnStore(tmp_path).create(**payload())["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create, range(16)))
    assert len(set(ids)) == 1

    def append(_):
        return TeamTurnStore(tmp_path).append_event(
            ids[0],
            "workspace",
            key="phase:planner",
            kind="phase_bound",
            data={"collaboration_id": "pc_" + "1" * 32},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        events = list(pool.map(append, range(16)))
    assert all(x == events[0] for x in events)
    assert len(TeamTurnStore(tmp_path).events(ids[0], "workspace")) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"team_version": 2},
        {"team_session_id": "different-team-session"},
        {"client_payload": {"content": "different"}},
        {"policy": {**payload()["policy"], "max_output_tokens": 1}},
        {
            "roster": {
                "teams": [{"agent_id": TEAM, "agent_version": 1, "member_ids": [CHILD]}],
                "agents": [{"agent_id": CHILD, "agent_version": 3, "tool_names": ["file_read"]}],
            }
        },
    ],
)
def test_same_request_cannot_change_any_immutable_binding(tmp_path, change):
    store = TeamTurnStore(tmp_path)
    store.create(**payload())
    if "team_version" in change:
        change = copy.deepcopy(change)
        change["roster"] = payload()["roster"]
        change["roster"]["teams"][0]["agent_version"] = 2
    with pytest.raises(PersonalAgentError) as caught:
        store.create(**payload(**change))
    assert caught.value.code == "idempotency_conflict"


def test_workspace_filter_source_namespace_and_order(tmp_path):
    store = TeamTurnStore(tmp_path)
    first = store.create(**payload())
    second = store.create(**payload(request_id="other", team_session_id="second"))
    store.create(**payload(workspace_key="other-workspace"))
    assert [x["id"] for x in store.list("workspace", oldest_first=True)] == [
        first["id"],
        second["id"],
    ]
    assert store.list("workspace", limit=1)[0]["id"] == second["id"]
    assert store.list("workspace", offset=1)[0]["id"] == first["id"]
    assert store.list("workspace", team_session_id="team-session") == [first]
    with pytest.raises(PersonalAgentError) as caught:
        store.get(first["id"], "other-workspace")
    assert caught.value.code == "not_found"


def test_nested_roster_closure_is_validated_and_canonical(tmp_path):
    roster = {
        "teams": [
            {"agent_id": TEAM, "agent_version": 1, "member_ids": [OTHER]},
            {"agent_id": OTHER, "agent_version": 1, "member_ids": [CHILD]},
        ],
        "agents": [
            {"agent_id": CHILD, "agent_version": 1, "tool_names": ["file_write", "file_read"]}
        ],
    }
    store = TeamTurnStore(tmp_path)
    turn = store.create(**payload(roster=roster))
    assert turn["roster"]["agents"][0]["tool_names"] == ["file_read", "file_write"]
    reverse = copy.deepcopy(roster)
    reverse["teams"].reverse()
    assert store.create(**payload(roster=reverse))["id"] == turn["id"]


@pytest.mark.parametrize("mutation", ["cycle", "unreachable", "duplicate", "wrong_root", "extra"])
def test_invalid_roster_never_creates_database(tmp_path, mutation):
    value = payload()
    roster = value["roster"]
    if mutation == "cycle":
        roster["teams"][0]["member_ids"] = [TEAM]
    if mutation == "unreachable":
        roster["agents"].append({"agent_id": OTHER, "agent_version": 1, "tool_names": []})
    if mutation == "duplicate":
        roster["agents"].append(copy.deepcopy(roster["agents"][0]))
    if mutation == "wrong_root":
        roster["teams"][0]["agent_version"] = 2
    if mutation == "extra":
        roster["teams"][0]["instructions"] = "not a roster field"
    store = TeamTurnStore(tmp_path)
    with pytest.raises(PersonalAgentError) as caught:
        store.create(**value)
    assert caught.value.code == "invalid_input" and not store.db_path.exists()


@pytest.mark.parametrize("scope", ["empty", "subset", "nested-empty"])
def test_membership_graph_can_include_ungranted_leaves_and_replay_events(tmp_path, scope):
    roster = payload()["roster"]
    if scope == "empty":
        roster["agents"] = []
    elif scope == "subset":
        roster["teams"][0]["member_ids"].append(OTHER)
    else:
        roster = {
            "teams": [
                {"agent_id": TEAM, "agent_version": 1, "member_ids": [OTHER]},
                {"agent_id": OTHER, "agent_version": 3, "member_ids": [CHILD]},
            ],
            "agents": [],
        }
    store = TeamTurnStore(tmp_path)
    turn = store.create(**payload(roster=roster))
    assert turn["roster"]["agents"] == roster["agents"]
    event = store.append_event(
        turn["id"],
        "workspace",
        key="scope-frozen",
        kind="scope_frozen",
        data={"granted_ids": [item["agent_id"] for item in roster["agents"]]},
    )
    reopened = TeamTurnStore(tmp_path)
    assert reopened.create(**payload(roster=roster))["id"] == turn["id"]
    assert (
        reopened.append_event(
            turn["id"],
            "workspace",
            key=event["key"],
            kind=event["kind"],
            data=event["data"],
        )
        == event
    )
    assert reopened.events(turn["id"], "workspace")[-1] == event


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": (1, 2)},
        {1: "bad key"},
        {"x": b"bytes"},
        {"x": "\ud800"},
        {"x": "bad\x00text"},
        {"x": object()},
    ],
)
def test_invalid_json_never_writes(tmp_path, value):
    store = TeamTurnStore(tmp_path)
    with pytest.raises(PersonalAgentError) as caught:
        store.create(**payload(client_payload=value))
    assert caught.value.code == "invalid_input" and not store.db_path.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("permission_mode", {}),
        ("permission_mode", "unknown"),
        ("max_turns", True),
        ("max_turns", 0),
        ("max_turns", 1001),
        ("max_budget_usd", -1),
        ("max_budget_usd", True),
        ("max_budget_usd", float("nan")),
        ("max_budget_usd", 1e9 + 1),
        ("max_input_tokens", True),
        ("max_output_tokens", -1),
    ],
)
def test_invalid_policy_never_writes(tmp_path, field, value):
    args = payload()
    args["policy"][field] = value
    store = TeamTurnStore(tmp_path)
    with pytest.raises(PersonalAgentError) as caught:
        store.create(**args)
    assert caught.value.code == "invalid_input" and not store.db_path.exists()


def test_policy_upper_bounds_match_team_models_and_worker_contract(tmp_path):
    args = payload()
    args["policy"].update(max_turns=1000, max_budget_usd=1e9)
    actual = TeamTurnStore(tmp_path).create(**args)
    assert actual["policy"]["max_turns"] == 1000
    assert actual["policy"]["max_budget_usd"] == 1e9


def test_size_depth_and_event_input_are_bounded(tmp_path):
    store = TeamTurnStore(tmp_path)
    with pytest.raises(PersonalAgentError):
        store.create(**payload(client_payload={"content": "x" * 1024**2}))
    nested = {}
    for _ in range(35):
        nested = {"nested": nested}
    with pytest.raises(PersonalAgentError):
        store.create(**payload(client_payload=nested))
    turn = store.create(**payload())
    for kwargs in (
        {"key": "bad key", "kind": "phase", "data": {}},
        {"key": "ok", "kind": "phase", "data": {"data": (1, 2)}},
        {"key": "created-again", "kind": "created", "data": {}},
        {"key": "wrong-key", "kind": "cancel_requested", "data": {}},
        {"key": "ok", "kind": "phase", "data": {"body": "x" * 1024**2}},
    ):
        with pytest.raises(PersonalAgentError):
            store.append_event(turn["id"], "workspace", **kwargs)
    assert len(store.events(turn["id"], "workspace")) == 1


@pytest.mark.parametrize("tamper", ["body", "tail", "head", "sequence", "cancel", "turn"])
def test_corrupted_chain_or_turn_is_rejected(tmp_path, tamper):
    store = TeamTurnStore(tmp_path)
    turn = store.create(**payload())
    store.append_event(
        turn["id"], "workspace", key="phase", kind="phase_bound", data={"job": "original"}
    )
    with sqlite3.connect(store.db_path) as conn:
        if tamper == "body":
            conn.execute("UPDATE team_turn_events SET data_json='{}' WHERE sequence=2")
        if tamper == "tail":
            conn.execute("DELETE FROM team_turn_events WHERE sequence=2")
        if tamper == "head":
            conn.execute("UPDATE team_turns SET event_head_sha256='bad'")
        if tamper == "sequence":
            conn.execute("UPDATE team_turn_events SET sequence=3 WHERE sequence=2")
        if tamper == "cancel":
            conn.execute("UPDATE team_turns SET cancel_requested=1")
        if tamper == "turn":
            raw = conn.execute("SELECT turn_json FROM team_turns").fetchone()[0]
            body = json.loads(raw)
            body["client_payload"]["content"] = "forged"
            conn.execute("UPDATE team_turns SET turn_json=?", (json.dumps(body),))
    with pytest.raises(PersonalAgentError) as caught:
        store.get(turn["id"], "workspace")
    assert caught.value.code == "storage_error"


def test_missing_reads_do_not_create_a_database(tmp_path):
    store = TeamTurnStore(tmp_path / "absent")
    assert store.list("workspace") == []
    assert store.find_request("workspace", "source", "request") is None
    with pytest.raises(PersonalAgentError):
        store.get("pt_" + "1" * 32, "workspace")
    assert not store.base_dir.exists()


def test_event_replay_binds_json_types_not_python_equality(tmp_path):
    store = TeamTurnStore(tmp_path)
    turn = store.create(**payload())
    store.append_event(turn["id"], "workspace", key="typed", kind="fact", data={"value": True})
    with pytest.raises(PersonalAgentError) as caught:
        store.append_event(turn["id"], "workspace", key="typed", kind="fact", data={"value": 1})
    assert caught.value.code == "idempotency_conflict"


def test_future_schema_is_never_rewritten(tmp_path):
    store = TeamTurnStore(tmp_path)
    turn = store.create(**payload())
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("PRAGMA user_version=999")
    before = store.db_path.read_bytes()
    for operation in (
        lambda: store.get(turn["id"], "workspace"),
        lambda: store.create(**payload()),
    ):
        with pytest.raises(PersonalAgentError) as caught:
            operation()
        assert caught.value.code == "unsupported_schema"
        assert store.db_path.read_bytes() == before


@pytest.mark.parametrize(
    "kwargs", [{"limit": True}, {"limit": 0}, {"offset": -1}, {"oldest_first": 1}]
)
def test_list_rejects_invalid_pagination_even_before_storage(tmp_path, kwargs):
    with pytest.raises(PersonalAgentError):
        TeamTurnStore(tmp_path).list("workspace", **kwargs)
