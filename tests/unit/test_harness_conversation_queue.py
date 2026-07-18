from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3

import pytest

from naumi_agent.harness.store import (
    HARNESS_STORE_SCHEMA_VERSION,
    HarnessStore,
    HarnessStoreConflictError,
    HarnessStoreError,
)

T0 = "2026-07-18T00:00:00+00:00"
T1 = "2026-07-18T00:00:01+00:00"
T2 = "2026-07-18T00:00:02+00:00"
T3 = "2026-07-18T00:00:03+00:00"


async def _enqueue(
    store: HarnessStore,
    workspace,
    request_id: str,
    *,
    session_id: str = "session-1",
    client_id: str = "terminal-a",
    text: str | None = None,
    enqueued_at: str = T0,
):
    return await store.enqueue_conversation(
        workspace_root=workspace,
        session_id=session_id,
        request_id=request_id,
        client_id=client_id,
        text=text or f"消息 {request_id}",
        enqueued_at=enqueued_at,
    )


@pytest.mark.asyncio
async def test_queue_enqueue_is_idempotent_and_survives_reopen(tmp_path) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(db_path)

    first = await _enqueue(store, workspace, "submit-1", text=" 保留空格 ")
    replay = await _enqueue(
        store,
        workspace,
        "submit-1",
        client_id="terminal-b",
        text=" 保留空格 ",
        enqueued_at=T3,
    )
    reopened = await HarnessStore(db_path).list_queued_conversations(
        workspace_root=workspace,
        session_id="session-1",
    )

    assert replay == first
    assert reopened == (first,)
    assert first.text == " 保留空格 "
    with pytest.raises(HarnessStoreConflictError, match="不同排队消息"):
        await _enqueue(store, workspace, "submit-1", text="被篡改的重试")


@pytest.mark.asyncio
async def test_queue_reads_v14_client_bound_digest_compatibly(tmp_path) -> None:
    db_path = tmp_path / "harness.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(db_path)
    item = await _enqueue(store, workspace, "submit-legacy", text="旧摘要")
    legacy_payload = json.dumps(
        {
            "client_id": item.client_id,
            "request_id": item.request_id,
            "session_id": item.session_id,
            "text": item.text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    legacy_digest = hashlib.sha256(legacy_payload.encode("utf-8")).hexdigest()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE harness_conversation_queue SET payload_sha256 = ? "
            "WHERE request_id = ?",
            (legacy_digest, item.request_id),
        )
        db.commit()

    recovered = await HarnessStore(db_path).list_queued_conversations(
        workspace_root=workspace,
        session_id="session-1",
    )
    assert len(recovered) == 1
    assert recovered[0].text == "旧摘要"


@pytest.mark.asyncio
async def test_queue_promotion_preserves_peer_order_and_is_idempotent(tmp_path) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await _enqueue(store, workspace, "first", enqueued_at=T0)
    await _enqueue(store, workspace, "second", enqueued_at=T1)
    await _enqueue(store, workspace, "third", enqueued_at=T2)

    promoted = await store.promote_queued_conversation(
        workspace_root=workspace,
        session_id="session-1",
        request_id="third",
        updated_at=T3,
    )
    replay = await store.promote_queued_conversation(
        workspace_root=workspace,
        session_id="session-1",
        request_id="third",
        updated_at=T3,
    )
    queued = await store.list_queued_conversations(
        workspace_root=workspace,
        session_id="session-1",
    )

    assert promoted.position == replay.position == 1
    assert [item.request_id for item in queued] == ["third", "first", "second"]
    assert [item.position for item in queued] == [1, 2, 3]
    with pytest.raises(HarnessStoreConflictError, match="不存在"):
        await store.promote_queued_conversation(
            workspace_root=workspace,
            session_id="session-1",
            request_id="missing",
            updated_at=T3,
        )


@pytest.mark.asyncio
async def test_terminal_transition_compacts_queue_and_rejects_state_rewrite(
    tmp_path,
) -> None:
    store = HarnessStore(tmp_path / "harness.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await _enqueue(store, workspace, "first", enqueued_at=T0)
    await _enqueue(store, workspace, "second", enqueued_at=T1)
    await _enqueue(store, workspace, "third", enqueued_at=T2)

    finished = await store.finish_queued_conversation(
        workspace_root=workspace,
        session_id="session-1",
        request_id="second",
        state="completed",
        terminal_reason="run_completed",
        updated_at=T3,
    )
    replay = await store.finish_queued_conversation(
        workspace_root=workspace,
        session_id="session-1",
        request_id="second",
        state="completed",
        terminal_reason="run_completed",
        updated_at=T3,
    )
    queued = await store.list_queued_conversations(
        workspace_root=workspace,
        session_id="session-1",
    )

    assert finished == replay
    assert finished.state == "completed"
    assert [item.request_id for item in queued] == ["first", "third"]
    assert [item.position for item in queued] == [1, 2]
    with pytest.raises(HarnessStoreConflictError, match="已经终结"):
        await store.finish_queued_conversation(
            workspace_root=workspace,
            session_id="session-1",
            request_id="second",
            state="cancelled",
            terminal_reason="user_cancelled",
            updated_at=T3,
        )


@pytest.mark.asyncio
async def test_queue_concurrent_enqueue_is_bounded_ordered_and_isolated(tmp_path) -> None:
    db_path = tmp_path / "harness.db"
    store = HarnessStore(db_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    await asyncio.gather(*(
        _enqueue(HarnessStore(db_path), first, f"item-{index:02d}")
        for index in range(20)
    ))
    queued = await store.list_queued_conversations(
        workspace_root=first,
        session_id="session-1",
    )
    assert len(queued) == 20
    assert sorted(item.position for item in queued) == list(range(1, 21))
    with pytest.raises(HarnessStoreConflictError, match="20 条上限"):
        await _enqueue(store, first, "overflow")
    assert await store.list_queued_conversations(
        workspace_root=second,
        session_id="session-1",
    ) == ()
    assert await store.list_queued_conversations(
        workspace_root=first,
        session_id="another-session",
    ) == ()


@pytest.mark.asyncio
async def test_queue_rejects_invalid_input_clock_regression_and_tampering(
    tmp_path,
) -> None:
    db_path = tmp_path / "harness.db"
    store = HarnessStore(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await _enqueue(store, workspace, "submit-1", enqueued_at=T2)

    with pytest.raises(ValueError, match="不能为空"):
        await _enqueue(store, workspace, "empty", text="   ")
    with pytest.raises(ValueError, match="request_id"):
        await _enqueue(store, workspace, "bad/id")
    with pytest.raises(HarnessStoreConflictError, match="不能早于"):
        await store.promote_queued_conversation(
            workspace_root=workspace,
            session_id="session-1",
            request_id="submit-1",
            updated_at=T1,
        )

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE harness_conversation_queue SET text = 'tampered' "
            "WHERE request_id = 'submit-1'"
        )
        db.commit()
    with pytest.raises(HarnessStoreError, match="摘要校验失败"):
        await store.list_queued_conversations(
            workspace_root=workspace,
            session_id="session-1",
        )


@pytest.mark.asyncio
async def test_schema_v13_upgrades_without_resetting_existing_data(tmp_path) -> None:
    db_path = tmp_path / "harness.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
        db.execute("INSERT INTO legacy_marker VALUES ('preserved')")
        db.execute("PRAGMA user_version = 13")
        db.commit()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    await _enqueue(HarnessStore(db_path), workspace, "submit-1")

    with sqlite3.connect(db_path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        marker = db.execute("SELECT value FROM legacy_marker").fetchone()[0]
        queued = db.execute(
            "SELECT COUNT(*) FROM harness_conversation_queue"
        ).fetchone()[0]
    assert version == HARNESS_STORE_SCHEMA_VERSION == 14
    assert marker == "preserved"
    assert queued == 1
