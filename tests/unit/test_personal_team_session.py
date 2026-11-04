"""Team-owned conversation delivery is transactional and survives stale writers."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.memory.session import Session, SessionStore

TEAM = "pa_" + "a" * 32
OTHER = "pa_" + "b" * 32
_UNSET = object()


def store(tmp_path):
    return SessionStore(MemoryConfig(session_db_path=str(tmp_path / "sessions.db")))


def message(identity="message-one", content="团队真实结果", role="assistant"):
    return {
        "id": identity,
        "role": role,
        "content": content,
        "metadata": {"team_turn_id": "pt_" + "1" * 32},
    }


async def team(first):
    return await first.create_session(
        title="用户创建的团队",
        personal_agent_id=TEAM,
        personal_team_id=TEAM,
        workspace_root="workspace",
    )


async def append(first, session, delivery="delivery-one", messages=_UNSET, **kwargs):
    return await first.append_personal_messages_once(
        session.id,
        expected_agent_id=TEAM,
        delivery_id=delivery,
        messages=[message()] if messages is _UNSET else messages,
        **kwargs,
    )


async def test_team_marker_round_trip_and_duplicate(tmp_path):
    first = store(tmp_path)
    try:
        original = await team(first)
        await append(first, original)
        restored = await first.load(original.id)
        assert restored.personal_team_id == TEAM and restored.to_row()["personal_team_id"] == TEAM
        copied = await first.duplicate(original.id)
        assert copied.id != original.id and copied.personal_team_id == TEAM
        assert copied.messages == restored.messages
        copied.messages[0]["content"] = "local mutation"
        assert (await first.load(original.id)).messages[0]["content"] == "团队真实结果"
    finally:
        await first.close()
    reopened = store(tmp_path)
    try:
        assert (await reopened.load(original.id)).personal_team_id == TEAM
    finally:
        await reopened.close()


async def test_legacy_schema_migration_and_two_openers(tmp_path):
    now = datetime.now().isoformat()
    with sqlite3.connect(tmp_path / "sessions.db") as conn:
        conn.execute("""CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,model TEXT,
            messages TEXT,created_at TEXT,updated_at TEXT,status TEXT,total_tokens INTEGER,
            total_cost_usd REAL)""")
        conn.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)",
            ("legacy", "旧会话", "model", "[]", now, now, "active", 5, 0.25),
        )
    first, second = store(tmp_path), store(tmp_path)
    try:
        records = await asyncio.gather(first.load("legacy"), second.load("legacy"))
        assert all(x.personal_team_id == "" and x.total_tokens == 5 for x in records)
        records[0].personal_agent_id = records[0].personal_team_id = TEAM
        await first.save(records[0])
        await append(second, records[0], messages=[message("migrated")])
        assert (await first.load("legacy")).messages[0]["id"] == "migrated"
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize("clear_marker", ["", OTHER])
async def test_stale_save_preserves_latest_messages_usage_and_syncs_object(tmp_path, clear_marker):
    first, second = store(tmp_path), store(tmp_path)
    try:
        original = await team(first)
        stale = await second.load(original.id)
        await append(first, original, usage_tokens=25, usage_cost_usd=0.125)
        stale.personal_team_id = stale.personal_agent_id = clear_marker
        stale.messages = [{"role": "assistant", "content": "stale overwrite"}]
        stale.total_tokens = 0
        stale.total_cost_usd = 0
        stale.title = "用户改名"
        stale.workspace_root = "different-workspace"
        await second.save(stale)
        actual = await first.load(original.id)
        assert actual.messages[0]["content"] == "团队真实结果"
        assert actual.total_tokens == 25 and actual.total_cost_usd == 0.125
        assert actual.personal_team_id == actual.personal_agent_id == TEAM
        assert actual.title == "用户改名" and actual.workspace_root == "workspace"
        assert stale.messages == actual.messages and stale.total_tokens == 25
        assert stale.personal_team_id == TEAM and stale.workspace_root == "workspace"
    finally:
        await first.close()
        await second.close()


async def test_pre_marker_snapshot_cannot_erase_owned_append(tmp_path):
    first, second = store(tmp_path), store(tmp_path)
    try:
        session = await first.create_session(personal_agent_id=TEAM)
        stale = await second.load(session.id)
        session.personal_team_id = TEAM
        await first.save(session)
        await append(first, session)
        await second.save(stale)
        assert stale.personal_team_id == TEAM and len(stale.messages) == 1
    finally:
        await first.close()
        await second.close()


async def test_legacy_team_binding_is_marker_only_idempotent_cas(tmp_path):
    first, second = store(tmp_path), store(tmp_path)
    try:
        session = await first.create_session(personal_agent_id=TEAM)
        session.add_message("user", "并发前的真实对话")
        await first.save(session)
        before = await first.load(session.id)
        await asyncio.gather(
            first.bind_personal_team(session.id, TEAM), second.bind_personal_team(session.id, TEAM)
        )
        after = await first.load(session.id)
        assert after.personal_team_id == TEAM
        assert after.messages == before.messages
        assert after.updated_at == before.updated_at
        assert after.total_tokens == before.total_tokens
        with pytest.raises(ValueError):
            await first.bind_personal_team(session.id, OTHER)
        await first.archive(session.id)
        with pytest.raises(ValueError):
            await second.bind_personal_team(session.id, TEAM)
    finally:
        await first.close()
        await second.close()


async def test_same_delivery_concurrent_two_stores_exactly_once_and_restart(tmp_path):
    first, second = store(tmp_path), store(tmp_path)
    try:
        session = await team(first)
        await second.load(session.id)
        results = await asyncio.gather(
            *[
                append(first if n % 2 else second, session, usage_tokens=11, usage_cost_usd=0.2)
                for n in range(12)
            ]
        )
        assert results == [["message-one"]] * 12
        actual = await first.load(session.id)
        assert (
            len(actual.messages) == 1 and actual.total_tokens == 11 and actual.total_cost_usd == 0.2
        )
    finally:
        await first.close()
        await second.close()
    reopened = store(tmp_path)
    try:
        assert await append(reopened, session, usage_tokens=11, usage_cost_usd=0.2) == [
            "message-one"
        ]
        assert (await reopened.load(session.id)).total_tokens == 11
    finally:
        await reopened.close()


async def test_distinct_concurrent_deliveries_and_stale_save_do_not_lose_messages(tmp_path):
    first, second = store(tmp_path), store(tmp_path)
    try:
        session = await team(first)
        stale = await second.load(session.id)
        await asyncio.gather(
            *[
                append(
                    first if n % 2 else second,
                    session,
                    delivery=f"delivery-{n}",
                    messages=[message(f"message-{n}")],
                    usage_tokens=1,
                )
                for n in range(20)
            ],
            first.save(replace(stale, title="改名")),
        )
        actual = await first.load(session.id)
        assert {m["id"] for m in actual.messages} == {f"message-{n}" for n in range(20)}
        assert actual.total_tokens == 20
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize(
    "change",
    [
        {"content": "other"},
        {"role": "user"},
        {"metadata": {}},
        {"usage_tokens": 2},
        {"usage_cost_usd": 0.1},
    ],
)
async def test_delivery_identity_payload_conflict_leaves_history_unchanged(tmp_path, change):
    first = store(tmp_path)
    try:
        session = await team(first)
        await append(first, session)
        body = message()
        usage = {}
        for name, value in change.items():
            if name.startswith("usage"):
                usage[name] = value
            else:
                body[name] = value
        with pytest.raises(ValueError, match="不同内容"):
            await append(first, session, messages=[body], **usage)
        assert len((await first.load(session.id)).messages) == 1
        with pytest.raises(ValueError, match="其他交付"):
            await append(first, session, delivery="other-delivery")
    finally:
        await first.close()


async def test_archive_allows_exact_receipt_read_but_never_new_delivery_or_save_revival(tmp_path):
    first = store(tmp_path)
    try:
        session = await team(first)
        await append(first, session)
        await first.archive(session.id)
        assert await append(first, session) == ["message-one"]
        with pytest.raises(ValueError, match="归档"):
            await append(first, session, delivery="new", messages=[message("new")])
        await first.save(session)
        actual = await first.load(session.id)
        assert actual.status == "archived" and actual.archived_at is not None
        assert session.status == "archived" and session.archived_at == actual.archived_at
    finally:
        await first.close()


@pytest.mark.parametrize("retention", [False, True])
async def test_deleted_team_session_is_not_resurrected_by_append_or_stale_save(tmp_path, retention):
    first = store(tmp_path)
    try:
        session = await team(first)
        await append(first, session)
        if retention:
            await first.archive(session.id)
            assert await first.delete_if_archived(session.id)
        else:
            assert await first.delete(session.id)
        with pytest.raises(ValueError):
            await append(first, session)
        with pytest.raises(ValueError, match="已删除"):
            await first.save(session)
        session.personal_team_id = session.personal_agent_id = ""
        with pytest.raises(ValueError, match="已删除"):
            await first.save(session)
        assert await first.load(session.id) is None
    finally:
        await first.close()


@pytest.mark.parametrize("kind", ["unowned", "wrong-team", "execution", "engine"])
async def test_append_scope_checks_before_any_write(tmp_path, kind):
    first = store(tmp_path)
    try:
        if kind == "unowned":
            session = await first.create_session()
        elif kind == "execution":
            session = await first.create_session(
                personal_agent_id=TEAM, personal_execution_id="execute"
            )
        elif kind == "engine":
            session = await first.create_session(personal_agent_id=TEAM, engine="pi")
        else:
            session = await first.create_session(personal_agent_id=OTHER, personal_team_id=OTHER)
        with pytest.raises(ValueError, match="归属"):
            await append(first, session)
        assert (await first.load(session.id)).messages == []
    finally:
        await first.close()


@pytest.mark.parametrize(
    "updates",
    [
        {"personal_agent_id": OTHER, "personal_team_id": TEAM},
        {"personal_agent_id": TEAM, "personal_team_id": TEAM, "personal_execution_id": "execution"},
        {"personal_agent_id": TEAM, "personal_team_id": TEAM, "engine": "pi"},
        {"personal_agent_id": TEAM, "personal_team_id": []},
    ],
)
async def test_invalid_new_marker_pairs_are_rejected(tmp_path, updates):
    first = store(tmp_path)
    try:
        session = Session(**updates)
        with pytest.raises(ValueError):
            await first.save(session)
        assert await first.load(session.id) is None
    finally:
        await first.close()


async def test_team_and_execution_markers_are_mutually_exclusive_in_both_directions(tmp_path):
    first = store(tmp_path)
    try:
        session = await team(first)
        with pytest.raises(ValueError):
            await first.bind_personal_execution(session.id, TEAM, "execution")
        execution = await first.create_session(
            personal_agent_id=TEAM, personal_execution_id="execution"
        )
        execution.personal_team_id = TEAM
        execution.personal_execution_id = ""
        with pytest.raises(ValueError):
            await first.save(execution)
        assert (await first.load(execution.id)).personal_execution_id == "execution"
        assert (await first.load(session.id)).personal_execution_id == ""
    finally:
        await first.close()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        [{}],
        [message(role="tool")],
        [message(role={})],
        [message(content="")],
        [message(), message()],
        [{**message(), "unexpected": 1}],
        [{**message(), "metadata": {"bad": (1, 2)}}],
        [{**message(), "metadata": {"bad": float("nan")}}],
        [message(identity="bad id")],
        [message(content="bad\x00text")],
    ],
)
async def test_malformed_message_batches_are_rejected_without_changes(tmp_path, bad):
    first = store(tmp_path)
    try:
        session = await team(first)
        with pytest.raises(ValueError):
            await append(first, session, messages=bad)
        assert (await first.load(session.id)).messages == []
    finally:
        await first.close()


@pytest.mark.parametrize(
    "usage",
    [
        {"usage_tokens": True},
        {"usage_tokens": -1},
        {"usage_tokens": "1"},
        {"usage_cost_usd": True},
        {"usage_cost_usd": float("nan")},
        {"usage_cost_usd": -1},
    ],
)
async def test_bad_usage_is_rejected(tmp_path, usage):
    first = store(tmp_path)
    try:
        session = await team(first)
        with pytest.raises(ValueError):
            await append(first, session, **usage)
        assert (await first.load(session.id)).total_tokens == 0
    finally:
        await first.close()


async def test_ledger_write_failure_rolls_back_messages_and_usage(tmp_path):
    first = store(tmp_path)
    try:
        session = await team(first)
        db = await first._get_db()
        await db.execute("""CREATE TRIGGER fail_delivery
                            BEFORE INSERT ON personal_session_deliveries
                            BEGIN SELECT RAISE(ABORT,'controlled ledger failure'); END""")
        await db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            await append(first, session, usage_tokens=10)
        actual = await first.load(session.id)
        assert actual.messages == [] and actual.total_tokens == 0
        await db.execute("DROP TRIGGER fail_delivery")
        await db.commit()
        assert await append(first, session, usage_tokens=10) == ["message-one"]
    finally:
        await first.close()


async def test_same_delivery_replay_detects_tampered_missing_message(tmp_path):
    first = store(tmp_path)
    try:
        session = await team(first)
        await append(first, session)
        db = await first._get_db()
        await db.execute("UPDATE sessions SET messages='[]' WHERE id=?", (session.id,))
        await db.commit()
        with pytest.raises(ValueError, match="正文不一致"):
            await append(first, session)
    finally:
        await first.close()


async def test_cancelled_simple_write_rolls_back_before_next_delivery(tmp_path, monkeypatch):
    first = store(tmp_path)
    try:
        session = await team(first)
        db = await first._get_db()
        original_commit = db.commit
        entered = asyncio.Event()

        async def wait_before_commit():
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(db, "commit", wait_before_commit)
        task = asyncio.create_task(first.archive(session.id))
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(db, "commit", original_commit)
        assert (await first.load(session.id)).status == "active"
        assert not db.in_transaction
        assert await append(first, session) == ["message-one"]
    finally:
        await first.close()
