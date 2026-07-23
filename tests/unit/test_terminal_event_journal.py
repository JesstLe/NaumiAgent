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
