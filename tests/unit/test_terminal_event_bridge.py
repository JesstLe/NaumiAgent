from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.runtime.terminal_events import (
    TerminalEventJournalError,
    TerminalEventJournalStore,
)
from naumi_agent.ui.bridge import JsonlEngineBridge
from naumi_agent.ui.protocol import ServerEventType


class _Engine:
    def __init__(
        self,
        store: TerminalEventJournalStore,
        *,
        session_id: str = "session-bridge",
    ) -> None:
        self.terminal_event_store = store
        self._session = SimpleNamespace(id=session_id) if session_id else None
        self._config = SimpleNamespace(ui=SimpleNamespace(show_reasoning=False))

    def set_permission_confirmer(self, callback) -> None:
        self.permission_confirmer = callback

    def set_user_interaction_handler(self, callback) -> None:
        self.user_interaction_handler = callback


class _FailingWriter(io.StringIO):
    def write(self, value: str) -> int:
        raise OSError("simulated closed pipe")


def _store(tmp_path: Path) -> TerminalEventJournalStore:
    return TerminalEventJournalStore(
        tmp_path / "terminal-events.db",
        workspace_root=tmp_path,
    )


async def test_bridge_reuses_stable_receipt_cursor_across_process_instances(
    tmp_path: Path,
) -> None:
    payload = {
        "schema_version": 1,
        "receipt_id": "receipt-bridge",
        "run_id": "run-bridge",
        "outcome": "completed",
    }
    first_writer = io.StringIO()
    first_bridge = JsonlEngineBridge(
        _Engine(_store(tmp_path)),
        config_path="config.yaml",
    )
    first_bridge.bind_writer(first_writer)
    await first_bridge.emit(
        ServerEventType.COMPLETION_RECEIPT,
        payload,
        request_id="request-live",
    )

    resumed_writer = io.StringIO()
    resumed_bridge = JsonlEngineBridge(
        _Engine(_store(tmp_path)),
        config_path="config.yaml",
    )
    resumed_bridge.bind_writer(resumed_writer)
    await resumed_bridge.emit(
        ServerEventType.COMPLETION_RECEIPT,
        dict(payload),
        request_id="request-resume",
    )
    await resumed_bridge.emit(
        ServerEventType.HARNESS_RECEIPT,
        {
            "schema_version": 1,
            "run_id": "harness-run",
            "revision": 1,
            "status": "completed",
        },
        request_id="request-resume",
    )

    first = json.loads(first_writer.getvalue())
    resumed, harness = [
        json.loads(line) for line in resumed_writer.getvalue().splitlines()
    ]
    assert {
        "event_id": first["event_id"],
        "stream_id": first["stream_id"],
        "cursor": first["cursor"],
    } == {
        "event_id": resumed["event_id"],
        "stream_id": resumed["stream_id"],
        "cursor": resumed["cursor"],
    }
    assert first["request_id"] == "request-live"
    assert resumed["request_id"] == "request-resume"
    assert first["cursor"] == 1
    assert harness["stream_id"] == first["stream_id"]
    assert harness["cursor"] == 2


async def test_bridge_commits_receipt_before_writer_failure(
    tmp_path: Path,
) -> None:
    payload = {
        "schema_version": 1,
        "receipt_id": "receipt-write-failed",
        "run_id": "run-write-failed",
        "outcome": "completed",
    }
    store = _store(tmp_path)
    bridge = JsonlEngineBridge(_Engine(store), config_path="config.yaml")
    bridge.bind_writer(_FailingWriter())

    with pytest.raises(OSError, match="closed pipe"):
        await bridge.emit(ServerEventType.COMPLETION_RECEIPT, payload)

    durable = await store.get_by_idempotency_key(
        session_id="session-bridge",
        idempotency_key="completion:receipt-write-failed",
    )
    assert durable is not None
    assert durable.cursor == 1

    recovered_writer = io.StringIO()
    recovered = JsonlEngineBridge(
        _Engine(_store(tmp_path)),
        config_path="config.yaml",
    )
    recovered.bind_writer(recovered_writer)
    await recovered.emit(ServerEventType.COMPLETION_RECEIPT, payload)
    assert json.loads(recovered_writer.getvalue())["event_id"] == durable.event_id


async def test_bridge_does_not_journal_non_allowlisted_protocol_events(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    writer = io.StringIO()
    bridge = JsonlEngineBridge(_Engine(store), config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.emit(ServerEventType.UI_MESSAGE, {"type": "text", "content": "private"})

    record = json.loads(writer.getvalue())
    assert "event_id" not in record
    assert "stream_id" not in record
    assert "cursor" not in record
    assert await store.list_after(session_id="session-bridge", cursor=0) == []


async def test_bridge_fails_closed_when_receipt_has_no_session_boundary(
    tmp_path: Path,
) -> None:
    writer = io.StringIO()
    bridge = JsonlEngineBridge(
        _Engine(_store(tmp_path), session_id=""),
        config_path="config.yaml",
    )
    bridge.bind_writer(writer)

    with pytest.raises(TerminalEventJournalError, match="缺少会话边界"):
        await bridge.emit(
            ServerEventType.COMPLETION_RECEIPT,
            {
                "schema_version": 1,
                "receipt_id": "receipt-sessionless",
                "run_id": "run-sessionless",
                "outcome": "completed",
            },
        )
    assert writer.getvalue() == ""


async def test_bridge_persists_and_confirms_terminal_event_ack(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    record = await store.append(
        session_id="session-bridge",
        event_type="completion/receipt",
        criticality="terminal",
        idempotency_key="completion:receipt-ack",
        payload={
            "schema_version": 1,
            "receipt_id": "receipt-ack",
            "run_id": "run-ack",
            "outcome": "completed",
        },
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(_Engine(store), config_path="config.yaml")
    bridge.bind_writer(writer)

    await bridge.acknowledge_terminal_events(
        {
            "client_id": "tecli_0123456789abcdef01234567",
            "session_id": "session-bridge",
            "stream_id": record.stream_id,
            "cursor": record.cursor,
        },
        request_id="ack-1",
    )

    response = json.loads(writer.getvalue())
    assert response["type"] == "ack"
    assert response["request_id"] == "ack-1"
    assert response["payload"] == {
        "event": "terminal_events/ack",
        "session_id": "session-bridge",
        "stream_id": record.stream_id,
        "cursor": 1,
    }
