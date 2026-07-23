from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from naumi_agent.runtime.terminal_events import (
    TerminalEventJournalConflictError,
    TerminalEventJournalError,
    TerminalEventJournalStore,
)


def _store(
    tmp_path: Path,
    *,
    max_events_per_stream: int = 4096,
) -> TerminalEventJournalStore:
    return TerminalEventJournalStore(
        tmp_path / "terminal-events.db",
        workspace_root=tmp_path,
        max_events_per_stream=max_events_per_stream,
    )


async def test_terminal_event_identity_survives_store_restart_and_resend(
    tmp_path: Path,
) -> None:
    payload = {"receipt_id": "receipt-1", "run_id": "run-1", "outcome": "completed"}
    first = await _store(tmp_path).append(
        session_id="session-1",
        event_type="completion/receipt",
        criticality="terminal",
        idempotency_key="completion:receipt-1",
        payload=payload,
    )
    resent = await _store(tmp_path).append(
        session_id="session-1",
        event_type="completion/receipt",
        criticality="terminal",
        idempotency_key="completion:receipt-1",
        payload=dict(reversed(list(payload.items()))),
    )

    assert resent == first
    assert first.cursor == 1
    assert first.event_id.startswith("tev_")
    assert first.stream_id.startswith("tes_")
    assert await _store(tmp_path).list_after(session_id="session-1", cursor=0) == [
        first
    ]


async def test_terminal_event_journal_allocates_contiguous_concurrent_cursors(
    tmp_path: Path,
) -> None:
    stores = [_store(tmp_path), _store(tmp_path)]

    async def append(index: int):
        return await stores[index % 2].append(
            session_id="session-concurrent",
            event_type="completion/receipt",
            criticality="terminal",
            idempotency_key=f"completion:receipt-{index}",
            payload={
                "receipt_id": f"receipt-{index}",
                "run_id": f"run-{index}",
                "outcome": "completed",
            },
        )

    records = await asyncio.gather(*(append(index) for index in range(24)))

    assert sorted(record.cursor for record in records) == list(range(1, 25))
    assert len({record.stream_id for record in records}) == 1
    persisted = await _store(tmp_path).list_after(
        session_id="session-concurrent",
        cursor=0,
        limit=100,
    )
    assert [record.cursor for record in persisted] == list(range(1, 25))


async def test_terminal_event_journal_rejects_conflicting_semantic_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await store.append(
        session_id="session-conflict",
        event_type="harness/receipt",
        criticality="terminal",
        idempotency_key="harness:run-1:1",
        payload={"run_id": "run-1", "revision": 1, "status": "completed"},
    )

    with pytest.raises(TerminalEventJournalConflictError, match="拒绝覆盖"):
        await store.append(
            session_id="session-conflict",
            event_type="harness/receipt",
            criticality="terminal",
            idempotency_key="harness:run-1:1",
            payload={"run_id": "run-1", "revision": 1, "status": "failed"},
        )


async def test_terminal_event_journal_detects_persisted_payload_corruption(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await store.append(
        session_id="session-corrupt",
        event_type="completion/receipt",
        criticality="terminal",
        idempotency_key="completion:receipt-corrupt",
        payload={
            "receipt_id": "receipt-corrupt",
            "run_id": "run-corrupt",
            "outcome": "completed",
        },
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE terminal_events SET payload_json = ?",
            ('{"outcome":"failed"}',),
        )
        db.commit()

    with pytest.raises(TerminalEventJournalError, match="完整性校验失败"):
        await store.get_by_idempotency_key(
            session_id="session-corrupt",
            idempotency_key="completion:receipt-corrupt",
        )


async def test_terminal_event_journal_bounds_retention_without_reusing_cursor(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, max_events_per_stream=3)
    for index in range(5):
        await store.append(
            session_id="session-retention",
            event_type="completion/receipt",
            criticality="terminal",
            idempotency_key=f"completion:receipt-{index}",
            payload={
                "receipt_id": f"receipt-{index}",
                "run_id": f"run-{index}",
                "outcome": "completed",
            },
        )

    retained = await store.list_after(session_id="session-retention", cursor=0)
    assert [record.cursor for record in retained] == [3, 4, 5]

    next_record = await store.append(
        session_id="session-retention",
        event_type="completion/receipt",
        criticality="terminal",
        idempotency_key="completion:receipt-next",
        payload={
            "receipt_id": "receipt-next",
            "run_id": "run-next",
            "outcome": "completed",
        },
    )
    assert next_record.cursor == 6
    assert [
        record.cursor
        for record in await store.list_after(
            session_id="session-retention",
            cursor=0,
        )
    ] == [4, 5, 6]


async def test_terminal_event_replay_window_distinguishes_replay_gap_and_stream_change(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, max_events_per_stream=3)
    records = []
    for index in range(5):
        records.append(
            await store.append(
                session_id="session-window",
                event_type="completion/receipt",
                criticality="terminal",
                idempotency_key=f"completion:window-{index}",
                payload={"receipt_id": f"window-{index}", "run_id": f"run-{index}"},
            )
        )
    client_id = "tecli_0123456789abcdef01234567"
    await store.acknowledge(
        client_id=client_id,
        session_id="session-window",
        stream_id=records[-1].stream_id,
        cursor=3,
    )

    replay = await store.replay_window(
        client_id=client_id,
        session_id="session-window",
        cursor=3,
        expected_stream_id=records[-1].stream_id,
    )
    assert replay.gap is False
    assert replay.gap_reason == ""
    assert replay.earliest_cursor == 3
    assert replay.latest_cursor == 5
    assert [record.cursor for record in replay.records] == [4, 5]

    pruned_client_id = "tecli_111111111111111111111111"
    await store.acknowledge(
        client_id=pruned_client_id,
        session_id="session-window",
        stream_id=records[-1].stream_id,
        cursor=1,
    )

    pruned = await store.replay_window(
        client_id=pruned_client_id,
        session_id="session-window",
        cursor=1,
        expected_stream_id=records[-1].stream_id,
    )
    assert pruned.gap is True
    assert pruned.gap_reason == "retention_gap"
    assert pruned.records == ()

    wrong_stream = await store.replay_window(
        client_id=client_id,
        session_id="session-window",
        cursor=5,
        expected_stream_id="tes_000000000000000000000000",
    )
    assert wrong_stream.gap is True
    assert wrong_stream.gap_reason == "stream_mismatch"

    missing_ack = await store.replay_window(
        client_id="tecli_222222222222222222222222",
        session_id="session-window",
        cursor=5,
        expected_stream_id=records[-1].stream_id,
    )
    assert missing_ack.gap is True
    assert missing_ack.gap_reason == "ack_missing"


async def test_terminal_event_ack_is_durable_monotonic_and_stream_fenced(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    record = await store.append(
        session_id="session-ack",
        event_type="completion/receipt",
        criticality="terminal",
        idempotency_key="completion:ack-1",
        payload={"receipt_id": "ack-1", "run_id": "run-ack"},
    )
    second = await store.append(
        session_id="session-ack",
        event_type="completion/receipt",
        criticality="terminal",
        idempotency_key="completion:ack-2",
        payload={"receipt_id": "ack-2", "run_id": "run-ack"},
    )
    client_id = "tecli_0123456789abcdef01234567"

    first = await store.acknowledge(
        client_id=client_id,
        session_id="session-ack",
        stream_id=record.stream_id,
        cursor=second.cursor,
    )
    repeated = await _store(tmp_path).acknowledge(
        client_id=client_id,
        session_id="session-ack",
        stream_id=record.stream_id,
        cursor=second.cursor,
    )
    assert repeated.cursor == first.cursor == 2

    with pytest.raises(TerminalEventJournalConflictError, match="不得回退"):
        await store.acknowledge(
            client_id=client_id,
            session_id="session-ack",
            stream_id=record.stream_id,
            cursor=record.cursor,
        )
    with pytest.raises(TerminalEventJournalConflictError, match="不一致"):
        await store.acknowledge(
            client_id=client_id,
            session_id="session-ack",
            stream_id="tes_000000000000000000000000",
            cursor=1,
        )


async def test_terminal_event_journal_rejects_unsafe_or_invalid_input(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="不可进入"):
        await store.append(
            session_id="session-unsafe",
            event_type="ui/message",
            criticality="terminal",
            idempotency_key="message:1",
            payload={"content": "secret"},
        )
    with pytest.raises(ValueError, match="criticality"):
        await store.append(
            session_id="session-unsafe",
            event_type="completion/receipt",
            criticality="informational",
            idempotency_key="completion:receipt-1",
            payload={"receipt_id": "receipt-1"},
        )
