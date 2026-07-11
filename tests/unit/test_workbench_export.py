"""Tests for the workbench audit-event export and secret redaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from naumi_agent.workbench.export import (
    export_audit_events,
    redact_event,
)
from naumi_agent.workbench.models import EventSeverity
from naumi_agent.workbench.store import WorkbenchStore


def test_redact_event_masks_sensitive_payload_keys() -> None:
    event = {
        "id": "ev-1",
        "payload": {
            "token": "sk-secret-123",
            "api_key": "key-abc",
            "title": "可见",
            "nested": {"password": "hunter2", "ok": 1},
            "items": [{"secret": "s", "name": "a"}],
        },
    }
    redacted = redact_event(event)
    assert redacted["id"] == "ev-1"
    payload = redacted["payload"]
    assert payload["token"] == "[REDACTED]"
    assert payload["api_key"] == "[REDACTED]"
    assert payload["title"] == "可见"
    assert payload["nested"]["password"] == "[REDACTED]"
    assert payload["nested"]["ok"] == 1
    assert payload["items"][0]["secret"] == "[REDACTED]"
    assert payload["items"][0]["name"] == "a"


def test_redact_event_preserves_non_sensitive_values() -> None:
    event = {"payload": {"message": "hello", "count": 3, "flag": True}}
    redacted = redact_event(event)
    assert redacted["payload"] == {"message": "hello", "count": 3, "flag": True}


@pytest.mark.asyncio
async def test_export_audit_events_writes_json_with_redaction(
    tmp_path: Path,
) -> None:
    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    await store.append_event(
        session_id="s",
        type="issue.created",
        actor="Human",
        subject_id="task-1",
        payload={"title": "可见", "token": "sk-leak"},
        severity=EventSeverity.INFO,
    )
    await store.append_event(
        session_id="s",
        type="lease.claimed",
        actor="Agent",
        subject_id="task-1",
        payload={"detail": "B"},
        severity=EventSeverity.WARNING,
        correlation_id="corr-1",
    )

    out = tmp_path / "audit.json"
    result = await export_audit_events(
        store,
        "s",
        str(out),
        fmt="json",
    )

    assert result["count"] == 2
    assert result["format"] == "json"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 2
    # Newest first: warning event with correlation then info event.
    assert data[0]["severity"] == "warning"
    assert data[0]["correlation_id"] == "corr-1"
    assert data[1]["severity"] == "info"
    # Secret must be redacted, visible value preserved.
    assert data[1]["payload"]["token"] == "[REDACTED]"
    assert data[1]["payload"]["title"] == "可见"


@pytest.mark.asyncio
async def test_export_audit_events_supports_ndjson_and_severity_filter(
    tmp_path: Path,
) -> None:
    store = WorkbenchStore(str(tmp_path / "workbench.db"))
    await store.append_event(
        session_id="s",
        type="issue.created",
        actor="Human",
        subject_id="task-1",
        payload={"detail": "A"},
        severity=EventSeverity.INFO,
    )
    await store.append_event(
        session_id="s",
        type="lease.claimed",
        actor="Agent",
        subject_id="task-1",
        payload={"detail": "B"},
        severity=EventSeverity.CRITICAL,
    )

    out = tmp_path / "audit.ndjson"
    result = await export_audit_events(
        store,
        "s",
        str(out),
        severity="critical",
        fmt="ndjson",
    )

    assert result["count"] == 1
    lines = [
        line for line in out.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["severity"] == "critical"
    assert record["type"] == "lease.claimed"


def test_export_audit_events_rejects_unsupported_format(tmp_path: Path) -> None:
    import asyncio

    store = WorkbenchStore(str(tmp_path / "workbench.db"))

    with pytest.raises(ValueError, match="不支持的导出格式"):
        asyncio.run(
            export_audit_events(store, "s", str(tmp_path / "x.txt"), fmt="csv")
        )


def test_export_audit_events_rejects_invalid_severity(tmp_path: Path) -> None:
    import asyncio

    store = WorkbenchStore(str(tmp_path / "workbench.db"))

    with pytest.raises(ValueError, match="无效 severity"):
        asyncio.run(export_audit_events(store, "s", str(tmp_path / "x.json"), severity="bogus"))
