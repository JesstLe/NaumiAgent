"""Execution identity is durable, immutable, and independent of journal indexes."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from naumi_agent.api.routes.messages import _assert_writable_session
from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.personal import runtime
from naumi_agent.personal.models import PersonalAgentError

OWNER = "pa_" + "a" * 32
OTHER_OWNER = "pa_" + "b" * 32
EXECUTION = "peer-task-" + "1" * 32
OTHER_EXECUTION = "peer-task-" + "2" * 32


def _store(tmp_path):
    return SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))


def _legacy_db(tmp_path):
    path = tmp_path / "sessions.db"
    now = datetime.now().isoformat()
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
            messages TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            status TEXT NOT NULL, total_tokens INTEGER NOT NULL, total_cost_usd REAL NOT NULL
        )""")
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                "原有执行",
                "model",
                '[{"role":"user","content":"原始资料"}]',
                now,
                now,
                "active",
                42,
                0.25,
            ),
        )
    return path


@pytest.mark.asyncio
async def test_execution_identity_round_trip_reopen_and_listing(tmp_path):
    first = _store(tmp_path)
    try:
        session = await first.create_session(
            title="互询执行", personal_agent_id=OWNER, personal_execution_id=EXECUTION
        )
        session.add_message("assistant", "已读取指定资料")
        await first.save(session)
        assert session.to_row()["personal_execution_id"] == EXECUTION
    finally:
        await first.close()
    reopened = _store(tmp_path)
    try:
        stored = await reopened.load(session.id)
        assert stored.personal_execution_id == EXECUTION
        assert stored.personal_agent_id == OWNER
        assert stored.messages[-1]["content"] == "已读取指定资料"
        listed, count = await reopened.list_sessions()
        assert count == 1 and listed[0].personal_execution_id == EXECUTION
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_execution_additive_migration_preserves_legacy_payload(tmp_path):
    path = _legacy_db(tmp_path)
    first = _store(tmp_path)
    try:
        original = await first.load("legacy")
        assert original.personal_execution_id == ""
        assert original.messages[0]["content"] == "原始资料"
        assert original.total_tokens == 42 and original.total_cost_usd == 0.25
        original.personal_agent_id = OWNER
        await first.save(original)
        await first.bind_personal_execution("legacy", OWNER, EXECUTION)
        assert (await first.load("legacy")).personal_execution_id == EXECUTION
    finally:
        await first.close()
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        assert "personal_execution_id" in columns


@pytest.mark.asyncio
async def test_two_connections_can_migrate_the_same_legacy_database(tmp_path):
    _legacy_db(tmp_path)
    first, second = _store(tmp_path), _store(tmp_path)
    try:
        results = await asyncio.gather(first.load("legacy"), second.load("legacy"))
        assert all(value.messages[0]["content"] == "原始资料" for value in results)
        assert all(value.personal_execution_id == "" for value in results)
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_same_store_concurrent_first_use_has_one_initialized_connection(tmp_path):
    _legacy_db(tmp_path)
    store = _store(tmp_path)
    try:
        results = await asyncio.gather(*(store.load("legacy") for _ in range(12)))
        assert len(results) == 12
        assert all(result.personal_execution_id == "" for result in results)
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["", OTHER_EXECUTION])
async def test_stale_saves_cannot_clear_or_replace_execution_identity(tmp_path, replacement):
    store = _store(tmp_path)
    try:
        original = await store.create_session(
            personal_agent_id=OWNER, personal_execution_id=EXECUTION
        )
        stale = replace(original, personal_execution_id=replacement, title="另一个快照")
        await store.save(stale)
        assert stale.personal_execution_id == EXECUTION
        stored = await store.load(original.id)
        assert stored.personal_execution_id == EXECUTION
        assert stored.title == "另一个快照"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_binding_wins_over_a_stale_unmarked_second_connection(tmp_path):
    first, second = _store(tmp_path), _store(tmp_path)
    try:
        original = await first.create_session(personal_agent_id=OWNER)
        stale = await second.load(original.id)
        await first.bind_personal_execution(original.id, OWNER, EXECUTION)
        await second.save(stale)
        assert stale.personal_execution_id == EXECUTION
        assert (await first.load(original.id)).personal_execution_id == EXECUTION
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_concurrent_stale_saves_keep_original_execution_identity(tmp_path):
    first, second = _store(tmp_path), _store(tmp_path)
    try:
        original = await first.create_session(
            personal_agent_id=OWNER, personal_execution_id=EXECUTION
        )
        await second.load(original.id)
        stale = replace(original, personal_execution_id="")
        changed = replace(original, personal_execution_id=OTHER_EXECUTION)
        await asyncio.gather(first.save(stale), second.save(changed))
        assert stale.personal_execution_id == changed.personal_execution_id == EXECUTION
        assert (await first.load(original.id)).personal_execution_id == EXECUTION
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_duplicate_archive_and_resume_keep_execution_identity(tmp_path):
    store = _store(tmp_path)
    try:
        original = await store.create_session(
            personal_agent_id=OWNER, personal_execution_id=EXECUTION
        )
        original.add_message("user", "指定的共享资料")
        await store.save(original)
        copied = await store.duplicate(original.id)
        assert copied.id != original.id
        assert copied.personal_execution_id == EXECUTION
        assert copied.personal_agent_id == OWNER
        copied.messages[0]["content"] = "副本独立"
        assert (await store.load(original.id)).messages[0]["content"] == "指定的共享资料"
        assert (await store.load(copied.id)).personal_execution_id == EXECUTION
        await store.archive(original.id)
        assert (await store.resume(original.id)).personal_execution_id == EXECUTION
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_binding_changes_only_marker_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    try:
        original = await store.create_session(personal_agent_id=OWNER)
        original.add_message("assistant", "晚于旧快照的新消息")
        await store.save(original)
        before = (await store.load(original.id)).to_row()
        await store.bind_personal_execution(original.id, OWNER, EXECUTION)
        await store.bind_personal_execution(original.id, OWNER, EXECUTION)
        after = (await store.load(original.id)).to_row()
        assert after == {**before, "personal_execution_id": EXECUTION}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_binding_is_compare_and_set_and_never_changes_owner(tmp_path):
    store = _store(tmp_path)
    try:
        original = await store.create_session(personal_agent_id=OWNER)
        with pytest.raises(ValueError, match="归属"):
            await store.bind_personal_execution(original.id, OTHER_OWNER, EXECUTION)
        assert (await store.load(original.id)).personal_execution_id == ""
        await store.bind_personal_execution(original.id, OWNER, EXECUTION)
        with pytest.raises(ValueError, match="归属"):
            await store.bind_personal_execution(original.id, OWNER, OTHER_EXECUTION)
        stored = await store.load(original.id)
        assert stored.personal_execution_id == EXECUTION and stored.personal_agent_id == OWNER
        with pytest.raises(ValueError, match="归属"):
            await store.bind_personal_execution("missing", OWNER, EXECUTION)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_concurrent_different_bindings_have_exactly_one_winner(tmp_path):
    first, second = _store(tmp_path), _store(tmp_path)
    try:
        original = await first.create_session(personal_agent_id=OWNER)
        await second.load(original.id)
        results = await asyncio.gather(
            first.bind_personal_execution(original.id, OWNER, EXECUTION),
            second.bind_personal_execution(original.id, OWNER, OTHER_EXECUTION),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        winner = EXECUTION if results[0] is None else OTHER_EXECUTION
        assert (await first.load(original.id)).personal_execution_id == winner
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker", [None, 7, True, "has space", "tab\t", "line\n", "\x7f", "x" * 129]
)
async def test_invalid_execution_marker_rejected_before_database_creation(tmp_path, marker):
    store = _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="执行身份标记"):
            await store.save(Session(personal_execution_id=marker))
        assert not (tmp_path / "sessions.db").exists()
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [None, 7, True, "", "has space", "tab\t", "\x7f", "x" * 129])
async def test_invalid_bind_rejected_before_database_creation(tmp_path, marker):
    store = _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="执行编号"):
            await store.bind_personal_execution("missing", OWNER, marker)
        assert not (tmp_path / "sessions.db").exists()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unmarked_sessions_and_legacy_rows_remain_compatible(tmp_path):
    session = Session(title="日常会话")
    row = session.to_row()
    row.pop("personal_execution_id")
    assert Session.from_row(row).personal_execution_id == ""
    store = _store(tmp_path)
    try:
        await store.save(session)
        assert (await store.load(session.id)).personal_execution_id == ""
        assert (await store.duplicate(session.id)).personal_execution_id == ""
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_missing_message_and_collaboration_indexes_do_not_make_history_writable(tmp_path):
    store = _store(tmp_path)
    try:
        saved = await store.create_session(personal_agent_id=OWNER, personal_execution_id=EXECUTION)
        # The session DB is the only persistent store present after journal loss.
        assert sorted(path.name for path in tmp_path.iterdir()) == ["sessions.db"]
        engine = SimpleNamespace(session_store=store, _session=saved)
        with pytest.raises(HTTPException) as error:
            await _assert_writable_session(engine, saved.id)
        assert error.value.status_code == 409
        with pytest.raises(PersonalAgentError) as error:
            runtime.require_current_agent(engine)
        assert error.value.code == "execution_session_readonly"
        assert not (tmp_path / "messages.sqlite3").exists()
    finally:
        await store.close()


@pytest.mark.parametrize(
    ("task_id", "authorizer"), [("", None), (EXECUTION, None), (OTHER_EXECUTION, object())]
)
def test_execution_requires_both_exact_task_identity_and_live_authorizer(task_id, authorizer):
    engine = SimpleNamespace(
        _session=Session(personal_agent_id=OWNER, personal_execution_id=EXECUTION),
        _personal_collaboration_task_id=task_id,
        _personal_execution_authorizer=authorizer,
    )
    with pytest.raises(PersonalAgentError) as error:
        runtime.require_current_agent(engine)
    assert error.value.code == "execution_session_readonly"
