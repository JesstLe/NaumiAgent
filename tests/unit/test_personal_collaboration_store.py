"""Real SQLite plan/journal boundaries, including cross-instance concurrency."""

from __future__ import annotations

import copy
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from naumi_agent.personal.collaboration_store import CollaborationStore
from naumi_agent.personal.models import PersonalAgentError

AGENT_A = "pa_" + "a" * 32
AGENT_B = "pa_" + "b" * 32
WORKSPACE = "workspace-a"


def step(step_id: str = "research", **changes: Any) -> dict[str, Any]:
    return {
        "id": step_id,
        "agent_id": AGENT_A,
        "agent_version": 1,
        "task": "读取用户指定的资料，列出核对结果。",
        "depends_on": [],
        **changes,
    }


def create(store: CollaborationStore, **changes: Any) -> dict[str, Any]:
    return store.create(**{
        "workspace_key": WORKSPACE,
        "source_session_id": "main-session",
        "source_agent_id": "",
        "request_id": "request-1",
        "steps": [step()],
        "title": "核对资料",
        "context": "用户明确共享的上下文。",
        **changes,
    })


def assert_error(code: str, operation) -> None:
    with pytest.raises(PersonalAgentError) as caught:
        operation()
    assert caught.value.code == code
    assert any("\u4e00" <= letter <= "\u9fff" for letter in caught.value.message)


def test_construction_is_lazy_and_empty_list_does_not_seed_roles(tmp_path: Path) -> None:
    target = tmp_path / "personal"
    store = CollaborationStore(target)
    assert store.base_dir == target
    assert store.db_path == target / "collaborations.db"
    assert not target.exists()
    assert store.list(WORKSPACE) == []
    assert store.db_path.exists()
    assert not (target / "personal_agents.db").exists()
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables == {
            "collaborations", "collaboration_events", "collaboration_sessions",
            "collaboration_permission_payloads",
        }
        columns = {row[1] for row in db.execute("PRAGMA table_info(collaborations)")}
        assert not {"state", "status", "response", "result", "job_id"} & columns


def test_plan_and_execution_identities_are_stable_after_restart(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path)
    original = create(store, steps=[
        step(),
        step("review", agent_id=AGENT_B, agent_version=4, depends_on=["research"]),
    ])
    assert original["id"].startswith("pc_") and len(original["id"]) == 35
    assert original["cancel_requested"] is False
    assert original["created_at"] == original["updated_at"]
    assert len({entry["session_id"] for entry in original["steps"]}) == 2
    for entry in original["steps"]:
        assert entry["task_id"] == f"{original['id']}-{entry['id']}"
        assert len(entry["task_id"]) <= 128
        assert len(entry["session_id"]) == 32
        assert "job_id" not in entry
    reopened = CollaborationStore(tmp_path)
    assert reopened.get(original["id"], WORKSPACE) == original
    assert create(reopened, steps=[
        step(), step("review", agent_id=AGENT_B, agent_version=4, depends_on=["research"]),
    ]) == original
    assert reopened.list(WORKSPACE) == [original]
    for entry in original["steps"]:
        assert reopened.lookup_for_session(WORKSPACE, entry["session_id"]) == original
        assert reopened.lookup_for_session("other-workspace", entry["session_id"]) is None
    assert reopened.lookup_for_session(WORKSPACE, "not-a-collaboration-session") is None


def test_inputs_and_returned_json_are_detached_from_storage(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path)
    inputs = [step(), step("review", depends_on=["research"])]
    result = create(store, steps=inputs)
    saved = copy.deepcopy(result)
    inputs[0]["task"] = "Changed input"
    inputs[1]["depends_on"].clear()
    result["steps"][0]["task"] = "Changed output"
    result["steps"].clear()
    assert store.get(saved["id"], WORKSPACE) == saved
    listed = store.list(WORKSPACE)
    listed[0]["title"] = "Changed list result"
    assert store.get(saved["id"], WORKSPACE) == saved


@pytest.mark.parametrize("change", [
    {"title": "另一个任务"},
    {"context": "新的共享资料"},
    {"source_agent_id": AGENT_B},
    {"steps": [step(task="请发另一份结果")]},
    {"steps": [step(agent_version=2)]},
    {"steps": [step(agent_id=AGENT_B)]},
    {"steps": [step(), step("review", depends_on=["research"])]},
])
def test_request_identity_cannot_be_rebound(tmp_path: Path, change: dict[str, Any]) -> None:
    store = CollaborationStore(tmp_path)
    original = create(store)
    assert_error("idempotency_conflict", lambda: create(store, **change))
    assert store.list(WORKSPACE) == [original]


def test_request_namespace_is_scoped_by_workspace_and_source_session(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path)
    first = create(store)
    second = create(store, source_session_id="other-session")
    third = create(store, workspace_key="other-workspace")
    assert len({first["id"], second["id"], third["id"]}) == 3
    assert [entry["id"] for entry in store.list(WORKSPACE)] == [second["id"], first["id"]]
    assert store.list(WORKSPACE, source_session_id="main-session") == [first]
    assert store.list(WORKSPACE, source_session_id="not-a-session") == []
    assert store.list(WORKSPACE, limit=1) == [second]
    assert store.list(WORKSPACE, limit=1, offset=1) == [first]
    assert store.list(WORKSPACE, offset=2) == []
    assert store.list("other-workspace") == [third]


def test_dependency_sets_are_normalized_and_forward_references_work(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path)
    before = create(store, steps=[
        step("final", depends_on=["review", "research"]),
        step("research"),
        step("review", depends_on=["research"]),
    ])
    after = create(store, steps=[
        step("final", depends_on=["research", "review"]),
        step("research"),
        step("review", depends_on=["research"]),
    ])
    assert after == before
    assert before["steps"][0]["depends_on"] == ["research", "review"]


def test_explicit_context_and_task_whitespace_are_not_rewritten(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path)
    context = "\n  def source():\n    return 3\n"
    task = "\n  请核对以下代码：\n    print(3)\n"
    plan = create(store, context=context, steps=[step(task=task)], title="  核对资料  ")
    assert plan["title"] == "核对资料"
    assert plan["context"] == context
    assert plan["steps"][0]["task"] == task
    assert_error("idempotency_conflict", lambda: create(
        store, context=context.strip(), steps=[step(task=task)], title="核对资料",
    ))


@pytest.mark.parametrize("bad_steps", [
    None, (), [], "research", [step()] * 13,
    [{"id": "research"}], [{**step(), "state": "completed"}],
    [step(), step()],
    [step(id="")], [step(id="bad step")], [step(id="x" * 65)],
    [step(id=3)], [step(agent_id="not-a-personal-agent")],
    [step(agent_version=True)], [step(agent_version=0)], [step(agent_version=1.5)],
    [step(agent_version=2_147_483_648)],
    [step(task="")], [step(task=" \n ")], [step(task="x" * 16001)],
    [step(task="text\0more")], [step(task="\ud800")],
    [step(depends_on=())], [step(depends_on="other")],
    [step(depends_on=["research"])],
    [step(depends_on=["missing"])],
    [step(depends_on=["other", "other"]), step("other")],
    [step(depends_on=["bad dependency"])],
    [step("a", depends_on=["b"]), step("b", depends_on=["a"])],
    [step("a", depends_on=["b"]), step("b", depends_on=["c"]),
     step("c", depends_on=["a"])],
])
def test_invalid_plan_is_rejected_before_any_file_write(tmp_path: Path, bad_steps: Any) -> None:
    store = CollaborationStore(tmp_path / "missing")
    assert_error("invalid_input", lambda: create(store, steps=bad_steps))
    assert not store.base_dir.exists()


@pytest.mark.parametrize("change", [
    {"workspace_key": ""}, {"workspace_key": True},
    {"source_session_id": ""}, {"source_session_id": "s" * 129},
    {"source_session_id": "bad\0id"}, {"source_session_id": "\ud800"},
    {"source_agent_id": None}, {"source_agent_id": "main"},
    {"request_id": ""}, {"request_id": "带空格 请求"}, {"request_id": "x" * 129},
    {"title": "x" * 161}, {"context": "x" * 32001}, {"context": None},
])
def test_invalid_top_level_values_are_rejected(tmp_path: Path, change: dict[str, Any]) -> None:
    store = CollaborationStore(tmp_path)
    assert_error("invalid_input", lambda: create(store, **change))


def test_maximum_step_count_and_identifier_length_are_supported(tmp_path: Path) -> None:
    steps = [step("x" * 62 + f"{index:02d}") for index in range(12)]
    steps[-1]["depends_on"] = [entry["id"] for entry in steps[:-1]]
    plan = create(CollaborationStore(tmp_path), steps=steps)
    assert len(plan["steps"]) == 12
    assert len(plan["steps"][-1]["depends_on"]) == 11
    assert max(len(entry["task_id"]) for entry in plan["steps"]) <= 128


def test_cancel_records_only_persistent_intent_and_is_idempotent(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path)
    before = create(store)
    after = store.request_cancel(before["id"], WORKSPACE)
    assert after["cancel_requested"] is True
    assert after["created_at"] == before["created_at"]
    assert after["steps"] == before["steps"]
    assert "status" not in after
    assert after["updated_at"] >= before["updated_at"]
    assert store.request_cancel(before["id"], WORKSPACE) == after
    assert create(store) == after
    assert CollaborationStore(tmp_path).get(before["id"], WORKSPACE) == after
    assert store.events(before["id"], WORKSPACE) == []


def test_journal_is_ordered_idempotent_and_restart_persistent(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path)
    plan = create(store)
    data = {"task_id": plan["steps"][0]["task_id"], "agent_id": AGENT_A}
    first = store.append_event(
        plan["id"], WORKSPACE, key="research:admitted", kind="job_admitted",
        step_id="research", data=data,
    )
    second = store.append_event(
        plan["id"], WORKSPACE, key="research:tool:1", kind="tool_start",
        step_id="research", data={"tool_name": "file_read", "call_id": "tool-1"},
    )
    assert first["sequence"] == 1 and second["sequence"] == 2
    assert first["data"] == data
    assert store.append_event(
        plan["id"], WORKSPACE, key="research:admitted", kind="job_admitted",
        step_id="research", data=dict(reversed(list(data.items()))),
    ) == first
    data["agent_id"] = AGENT_B
    first["data"]["agent_id"] = "changed-output"
    reopened = CollaborationStore(tmp_path)
    saved = reopened.events(plan["id"], WORKSPACE)
    assert len(saved) == 2
    assert saved[0]["data"]["agent_id"] == AGENT_A
    assert saved[1] == second
    assert reopened.events(plan["id"], WORKSPACE, after=1) == [second]
    assert reopened.events(plan["id"], WORKSPACE, after=2) == []
    assert reopened.events(plan["id"], WORKSPACE, limit=1) == saved[:1]
    assert reopened.get(plan["id"], WORKSPACE) == plan


@pytest.mark.parametrize("change", [
    {"kind": "result"}, {"step_id": ""}, {"data": {"value": 2}},
])
def test_journal_key_cannot_be_rebound(tmp_path: Path, change: dict[str, Any]) -> None:
    store = CollaborationStore(tmp_path)
    plan = create(store)
    fields = {"key": "observation-1", "kind": "update", "step_id": "research",
              "data": {"value": 1}}
    original = store.append_event(plan["id"], WORKSPACE, **fields)
    assert_error("idempotency_conflict", lambda: store.append_event(
        plan["id"], WORKSPACE, **{**fields, **change},
    ))
    assert store.events(plan["id"], WORKSPACE) == [original]


@pytest.mark.parametrize("fields", [
    {"key": ""}, {"key": "x" * 257}, {"key": "包含 空格"},
    {"kind": ""}, {"kind": "x" * 65}, {"kind": "bad kind"},
    {"step_id": "unknown"}, {"step_id": "bad step"},
    {"data": []}, {"data": {1: "not-a-string-key"}},
    {"data": {"number": math.nan}}, {"data": {"number": math.inf}},
    {"data": {"object": object()}}, {"data": {"tuple": (1, 2)}},
    {"data": {"too_long": "中" * 23000}}, {"data": {"bad": "\ud800"}},
])
def test_invalid_event_does_not_append(tmp_path: Path, fields: dict[str, Any]) -> None:
    store = CollaborationStore(tmp_path)
    plan = create(store)
    assert_error("invalid_input", lambda: store.append_event(
        plan["id"], WORKSPACE, **{"key": "valid", "kind": "update", **fields},
    ))
    assert store.events(plan["id"], WORKSPACE) == []


def test_recursive_event_is_rejected_without_recursion_failure(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path)
    plan = create(store)
    recursive: dict[str, Any] = {}
    recursive["self"] = recursive
    assert_error("invalid_input", lambda: store.append_event(
        plan["id"], WORKSPACE, key="recursive", kind="update", data=recursive,
    ))
    assert store.events(plan["id"], WORKSPACE) == []


def test_scope_is_enforced_for_all_reads_and_writes(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path)
    plan = create(store)
    store.append_event(plan["id"], WORKSPACE, key="first", kind="update")
    for operation in (
        lambda: store.get(plan["id"], "other"),
        lambda: store.request_cancel(plan["id"], "other"),
        lambda: store.events(plan["id"], "other"),
        lambda: store.append_event(plan["id"], "other", key="other", kind="update"),
        lambda: store.get("pc_" + "f" * 32, WORKSPACE),
    ):
        assert_error("not_found", operation)
    assert store.list("other") == []
    assert store.lookup_for_session("other", plan["steps"][0]["session_id"]) is None
    assert store.get(plan["id"], WORKSPACE)["cancel_requested"] is False
    assert len(store.events(plan["id"], WORKSPACE)) == 1


@pytest.mark.parametrize("operation", [
    lambda store, identity: store.events(identity, WORKSPACE, after=-1),
    lambda store, identity: store.events(identity, WORKSPACE, after=True),
    lambda store, identity: store.events(identity, WORKSPACE, after=1.5),
    lambda store, identity: store.events(identity, WORKSPACE, limit=0),
    lambda store, identity: store.events(identity, WORKSPACE, limit=501),
    lambda store, identity: store.events(identity, WORKSPACE, limit=False),
    lambda store, identity: store.list(WORKSPACE, limit=0),
    lambda store, identity: store.list(WORKSPACE, limit=501),
    lambda store, identity: store.list(WORKSPACE, limit=True),
    lambda store, identity: store.list(WORKSPACE, offset=-1),
    lambda store, identity: store.list(WORKSPACE, offset=True),
    lambda store, identity: store.list(WORKSPACE, source_session_id=""),
    lambda store, identity: store.get("wrong-id", WORKSPACE),
    lambda store, identity: store.lookup_for_session(WORKSPACE, ""),
])
def test_query_bounds_are_strict(tmp_path: Path, operation) -> None:
    store = CollaborationStore(tmp_path)
    plan = create(store)
    assert_error("invalid_input", lambda: operation(store, plan["id"]))


def test_concurrent_duplicate_creates_share_one_plan_and_session_mapping(tmp_path: Path) -> None:
    def call(_: int) -> dict[str, Any]:
        return create(CollaborationStore(tmp_path))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(24)))
    assert all(result == results[0] for result in results)
    store = CollaborationStore(tmp_path)
    assert store.list(WORKSPACE) == results[:1]
    assert store.lookup_for_session(
        WORKSPACE, results[0]["steps"][0]["session_id"],
    ) == results[0]


def test_concurrent_events_are_gapless_and_duplicate_keys_do_not_duplicate(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path)
    plan = create(store)

    def call(index: int) -> dict[str, Any]:
        return CollaborationStore(tmp_path).append_event(
            plan["id"], WORKSPACE, key=f"event-{index % 20}", kind="update",
            step_id="research", data={"value": index % 20},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(60)))
    saved = store.events(plan["id"], WORKSPACE)
    assert len(saved) == 20
    assert [entry["sequence"] for entry in saved] == list(range(1, 21))
    by_key = {entry["key"]: entry for entry in saved}
    assert all(entry == by_key[entry["key"]] for entry in results)


def test_journal_sequences_are_per_collaboration(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path)
    first = create(store)
    second = create(store, request_id="second")
    for plan in (first, second):
        event = store.append_event(plan["id"], WORKSPACE, key="first", kind="update")
        assert event["sequence"] == 1


def test_future_schema_is_not_downgraded(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path)
    store.list(WORKSPACE)
    with sqlite3.connect(store.db_path) as db:
        db.execute("PRAGMA user_version=999")
    assert_error("unsupported_schema", lambda: CollaborationStore(tmp_path).list(WORKSPACE))
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 999


def test_unavailable_directory_reports_chinese_storage_error(tmp_path: Path) -> None:
    occupied = tmp_path / "file-not-directory"
    occupied.write_text("existing", encoding="utf-8")
    assert_error("storage_error", lambda: CollaborationStore(occupied).list(WORKSPACE))
    assert occupied.read_text(encoding="utf-8") == "existing"
