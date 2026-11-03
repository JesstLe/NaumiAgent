"""Real SQLite contracts for dynamically provisioned personal agents."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path

import pytest

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.store import PersonalAgentStore


@pytest.fixture
def store(tmp_path: Path) -> PersonalAgentStore:
    return PersonalAgentStore(tmp_path / "personal")


def create(store: PersonalAgentStore, request: str = "create-1", **fields):
    return store.create(
        **{
            "name": "资料助理",
            "instructions": "读取用户指定资料并注明来源",
            "request_id": request,
            **fields,
        }
    )


def assert_code(code: str):
    return _CodeContext(code)


class _CodeContext:
    def __init__(self, code: str):
        self.code = code
        self.context = pytest.raises(PersonalAgentError)

    def __enter__(self):
        return self.context.__enter__()

    def __exit__(self, kind, value, traceback):
        result = self.context.__exit__(kind, value, traceback)
        assert value.code == self.code
        assert value.message and str(value) == value.message
        return result


def test_catalog_starts_empty_and_constructor_does_no_io(store: PersonalAgentStore):
    assert not store.base_dir.exists()
    assert store.list() == ()
    assert store.db_path.is_file()
    assert PersonalAgentStore(store.base_dir).list() == ()
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_create_restart_immutable_dto_and_explicit_tool_scope(store: PersonalAgentStore):
    agent = create(store, tool_names=["web_search", "file_read"])
    assert agent.id.startswith("pa_") and len(agent.id) == 35
    assert agent.status == "active" and agent.version == 1
    assert agent.tool_names == ("file_read", "web_search")
    assert agent.member_ids == ()
    assert datetime.fromisoformat(agent.created_at).utcoffset().total_seconds() == 0
    assert agent.updated_at == agent.created_at
    reopened = PersonalAgentStore(store.base_dir)
    assert reopened.get(agent.id) == agent
    assert reopened.list() == (agent,)
    with pytest.raises(FrozenInstanceError):
        agent.name = "篡改"
    data = agent.to_dict()
    data["tool_names"].append("bash_run")
    assert agent.tool_names == ("file_read", "web_search")
    assert create(store, "empty-tools").tool_names == ()


@pytest.mark.parametrize(
    "fields",
    [
        {"name": ""},
        {"name": " \n "},
        {"name": "x" * 81},
        {"name": None},
        {"instructions": ""},
        {"instructions": "x" * 12001},
        {"instructions": 5},
        {"description": "x" * 501},
        {"description": None},
        {"name": "bad\x00name"},
        {"model_tier": "unknown"},
        {"kind": "admin"},
        {"tool_names": "bash_run"},
        {"tool_names": ["file_read", "file_read"]},
        {"tool_names": ["bad tool"]},
        {"tool_names": ["x"] * 129},
        {"tool_names": [None]},
        {"member_ids": "pa_id"},
        {"request_id": ""},
        {"request_id": "bad request"},
        {"request_id": "x" * 129},
    ],
)
def test_invalid_creation_never_creates_agent(store: PersonalAgentStore, fields):
    with assert_code("invalid_input"):
        create(store, **fields)
    assert store.list() == ()


def test_boundary_lengths_unicode_and_multiline_instructions(store: PersonalAgentStore):
    agent = create(
        store,
        name="界" * 80,
        description="描" * 500,
        instructions="说\n" + "明" * 11998,
        tool_names=[f"tool_{n}" for n in range(128)],
    )
    assert len(agent.instructions) == 12000
    assert len(agent.tool_names) == 128
    assert agent.name == "界" * 80


def test_idempotent_create_uses_normalized_payload_and_does_not_resurrect(store):
    first = create(store, name="  资料助理  ", tool_names=["web_search", "file_read"])
    replay = create(store, tool_names=["file_read", "web_search"])
    assert replay == first
    archived = store.update(first.id, first.version, status="archived")
    assert create(store, tool_names=["file_read", "web_search"]) == archived
    assert len(store.history(first.id)) == 2
    assert store.list() == ()
    assert store.list(include_archived=True) == (archived,)
    with assert_code("idempotency_conflict"):
        create(store, instructions="不同的职责")
    assert len(store.list(include_archived=True)) == 1


def test_create_request_reused_across_two_instances_is_one_agent(store):
    def worker(_):
        return create(PersonalAgentStore(store.base_dir))

    with ThreadPoolExecutor(max_workers=8) as executor:
        profiles = list(executor.map(worker, range(16)))
    assert len({a.id for a in profiles}) == 1
    assert len(store.list()) == 1
    assert len(store.history(profiles[0].id)) == 1


def test_concurrent_conflicting_request_does_not_silently_change_contents(store):
    def worker(i):
        try:
            return create(PersonalAgentStore(store.base_dir), name=f"专员{i}")
        except PersonalAgentError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(worker, range(4)))
    assert sum(result == "idempotency_conflict" for result in results) == 3
    assert len(store.list()) == 1


def test_edit_history_archive_resume_and_noop(store):
    first = create(store)
    changed = store.update(
        first.id,
        1,
        name="研究专员",
        instructions="先验证再回答",
        model_tier="reasoning",
        tool_names=["web_search"],
    )
    assert changed.version == 2 and changed.created_at == first.created_at
    assert changed.instructions == "先验证再回答"
    assert changed.updated_at >= first.updated_at
    assert store.update(first.id, 2, name=changed.name) == changed
    assert len(store.history(first.id)) == 2
    paused = store.update(first.id, 2, status="paused")
    archived = store.update(first.id, paused.version, status="archived")
    resumed = store.update(first.id, archived.version, status="active")
    assert resumed.version == 5
    history = store.history(first.id)
    assert [x.version for x in history] == [1, 2, 3, 4, 5]
    assert history[0] == first and history[-1] == resumed
    assert PersonalAgentStore(store.base_dir).history(first.id) == history


@pytest.mark.parametrize(
    "version,fields,code",
    [
        (0, {"name": "修改"}, "invalid_input"),
        (True, {"name": "修改"}, "invalid_input"),
        (1, {}, "invalid_input"),
        (1, {"kind": "team"}, "invalid_input"),
        (1, {"id": "other"}, "invalid_input"),
        (1, {"status": "deleted"}, "invalid_input"),
        (1, {"instructions": None}, "invalid_input"),
        (2, {"name": "修改"}, "version_conflict"),
    ],
)
def test_update_rejects_invalid_or_stale_fields(store, version, fields, code):
    agent = create(store)
    with assert_code(code):
        store.update(agent.id, version, **fields)
    assert store.get(agent.id) == agent
    assert len(store.history(agent.id)) == 1


def test_concurrent_optimistic_updates_have_one_winner(store):
    agent = create(store)

    def worker(i):
        try:
            return PersonalAgentStore(store.base_dir).update(agent.id, 1, name=f"新名称{i}")
        except PersonalAgentError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(worker, range(6)))
    assert sum(result == "version_conflict" for result in results) == 5
    assert store.get(agent.id).version == 2
    assert len(store.history(agent.id)) == 2


def test_team_member_validation_cycles_and_atomic_history(store):
    specialist = create(store)
    first = create(store, "team-1", kind="team", member_ids=[specialist.id])
    second = create(store, "team-2", kind="team", member_ids=[first.id])
    with assert_code("member_cycle"):
        store.update(first.id, 1, member_ids=[second.id])
    with assert_code("member_cycle"):
        store.update(first.id, 1, member_ids=[first.id])
    with assert_code("invalid_members"):
        store.update(specialist.id, 1, member_ids=[first.id])
    with assert_code("member_not_found"):
        create(store, "missing-team", kind="team", member_ids=["pa_" + "0" * 32])
    with assert_code("invalid_input"):
        store.update(first.id, 1, member_ids=[specialist.id, specialist.id])
    assert store.get(first.id) == first
    assert len(store.history(first.id)) == 1
    assert len(store.list()) == 3


def test_inactive_member_rejected_and_explicit_team_reactivation_revalidates(store):
    specialist = create(store)
    team = create(store, "team", kind="team", member_ids=[specialist.id])
    paused_team = store.update(team.id, 1, status="paused")
    inactive = store.update(specialist.id, 1, status="archived")
    with assert_code("member_inactive"):
        create(store, "invalid-team", kind="team", member_ids=[specialist.id])
    with assert_code("member_inactive"):
        store.update(team.id, paused_team.version, status="active")
    assert store.get(team.id) == paused_team
    store.update(inactive.id, inactive.version, status="active")
    assert store.update(team.id, paused_team.version, status="active").status == "active"


def test_empty_team_can_be_configured_later_but_is_not_seeded(store):
    team = create(store, kind="team")
    assert team.member_ids == ()
    assert store.list() == (team,)


def test_binding_get_or_create_is_stable_and_requires_cas_for_recovery(store):
    agent = create(store)
    assert store.get_binding(agent.id, "workspace-a") is None
    initial = store.bind_session(agent.id, "workspace-a", "session-1", session_db_key="db-1")
    assert initial.session_db_key == "db-1"
    assert (
        store.bind_session(agent.id, "workspace-a", "session-2", session_db_key="db-1") == initial
    )
    with assert_code("binding_conflict"):
        store.bind_session(agent.id, "workspace-a", "session-2", expected_session_id="wrong")
    replaced = store.bind_session(
        agent.id, "workspace-a", "session-2", session_db_key="db-2", expected_session_id="session-1"
    )
    assert replaced.created_at == initial.created_at
    assert replaced.session_id == "session-2" and replaced.session_db_key == "db-2"
    assert store.agent_for_session("workspace-a", "session-1") == agent
    assert store.agent_for_session("workspace-a", "session-2", session_db_key="db-2") == agent
    assert store.agent_for_session("workspace-a", "session-2", session_db_key="db-1") is None
    assert PersonalAgentStore(store.base_dir).get_binding(agent.id, "workspace-a") == replaced
    assert (
        store.bind_session(
            agent.id,
            "workspace-a",
            "session-2",
            session_db_key="db-2",
            expected_session_id="session-2",
        )
        == replaced
    )


def test_cas_absent_binding_rejected(store):
    agent = create(store)
    with assert_code("binding_conflict"):
        store.bind_session(agent.id, "workspace", "new", expected_session_id="old")
    assert store.get_binding(agent.id, "workspace") is None


def test_concurrent_binding_returns_one_winner_without_overwriting(store):
    agent = create(store)

    def worker(i):
        return PersonalAgentStore(store.base_dir).bind_session(
            agent.id, "workspace", f"session-{i}", session_db_key="db"
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(worker, range(8)))
    assert len(set(results)) == 1
    assert store.get_binding(agent.id, "workspace") == results[0]


def test_session_cannot_belong_to_two_agents_and_db_keys_are_unambiguous(store):
    one = create(store)
    two = create(store, "two")
    store.bind_session(one.id, "workspace", "session", session_db_key="db-a")
    with assert_code("session_conflict"):
        store.bind_session(two.id, "workspace", "session", session_db_key="db-a")
    store.bind_session(two.id, "workspace", "session", session_db_key="db-b")
    with assert_code("ambiguous_session"):
        store.agent_for_session("workspace", "session")
    assert store.agent_for_session("workspace", "session", session_db_key="db-a") == one
    assert store.agent_for_session("workspace", "session", session_db_key="db-b") == two
    store.bind_session(two.id, "other-workspace", "session", session_db_key="db-a")
    assert store.agent_for_session("other-workspace", "session") == two


def test_archiving_preserves_binding_but_blocks_opening_and_is_discoverable(store):
    agent = create(store)
    binding = store.bind_session(agent.id, "workspace", "session")
    archived = store.update(agent.id, 1, status="archived")
    assert store.get_binding(agent.id, "workspace") == binding
    assert store.agent_for_session("workspace", "session") == archived
    with assert_code("agent_inactive"):
        store.bind_session(agent.id, "workspace", "different")
    active = store.update(agent.id, archived.version, status="active")
    assert store.bind_session(active.id, "workspace", "different") == binding


def test_recovered_session_cannot_lose_policy_or_be_claimed_by_another_agent(store):
    one = create(store)
    two = create(store, "two")
    store.bind_session(one.id, "workspace", "old-session", session_db_key="db")
    store.bind_session(
        one.id, "workspace", "new-session", session_db_key="db", expected_session_id="old-session"
    )
    restarted = PersonalAgentStore(store.base_dir)
    assert restarted.agent_for_session("workspace", "old-session", session_db_key="db") == one
    with assert_code("session_conflict"):
        restarted.bind_session(two.id, "workspace", "old-session", session_db_key="db")
    assert restarted.get_binding(two.id, "workspace") is None


def test_missing_and_invalid_ids(store):
    unknown = "pa_" + "a" * 32
    assert store.get(unknown) is None
    with assert_code("not_found"):
        store.update(unknown, 1, name="other")
    with assert_code("not_found"):
        store.history(unknown)
    with assert_code("not_found"):
        store.bind_session(unknown, "workspace", "session")
    for value in ("", "../escape", "pa_invalid", None):
        with assert_code("invalid_input"):
            store.get(value)


def test_newer_database_schema_is_not_downgraded(store):
    store.base_dir.mkdir()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("PRAGMA user_version=99")
    with assert_code("unsupported_schema"):
        store.list()
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99


def test_broken_database_returns_actionable_storage_error(store):
    store.base_dir.mkdir()
    store.db_path.write_bytes(b"not a sqlite file")
    with assert_code("storage_error"):
        store.list()


def test_independent_owner_catalogs_do_not_share_agents(tmp_path):
    alice = PersonalAgentStore(tmp_path / "alice")
    bob = PersonalAgentStore(tmp_path / "bob")
    agent = create(alice)
    assert bob.list() == () and bob.get(agent.id) is None
