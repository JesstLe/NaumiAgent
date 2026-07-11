"""Audit-event export helpers for the workbench timeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from naumi_agent.workbench.models import EventSeverity
from naumi_agent.workbench.store import WorkbenchStore

_SENSITIVE_KEYS = {
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "credential",
    "bearer",
    "auth",
    "authorization",
    "private_key",
}


def _is_sensitive_key(key: str) -> bool:
    """True when a payload key likely holds a secret."""
    lower = key.lower()
    return any(term in lower for term in _SENSITIVE_KEYS)


def _redact_value(value: Any) -> Any:
    """Recursively redact values for sensitive-looking keys."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_key(key) else _redact_value(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def redact_event(event_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an event dict with payload secrets redacted."""
    result = dict(event_dict)
    result["payload"] = _redact_value(result.get("payload", {}))
    return result


def _coerce_severity(severity: str | None) -> EventSeverity | None:
    """Validate a severity filter value."""
    if severity is None:
        return None
    try:
        return EventSeverity(severity)
    except ValueError as exc:
        raise ValueError(f"无效 severity: {severity}") from exc


async def export_audit_events(
    store: WorkbenchStore,
    session_id: str,
    output_path: str,
    *,
    event_type: str | None = None,
    subject_id: str | None = None,
    actor: str | None = None,
    severity: str | None = None,
    correlation_id: str | None = None,
    parent_event_id: str | None = None,
    since: str | None = None,
    limit: int = 10000,
    fmt: str = "json",
) -> dict[str, Any]:
    """Export audit events to a local file, redacting secrets.

    Supported formats: ``json`` (pretty-printed array) and ``ndjson`` (one object
    per line).
    """
    if fmt not in {"json", "ndjson"}:
        raise ValueError(f"不支持的导出格式: {fmt}")

    severity_value = _coerce_severity(severity)
    events = await store.list_events(
        session_id,
        event_type=event_type,
        subject_id=subject_id,
        actor=actor,
        since=since,
        severity=severity_value.value if severity_value is not None else None,
        correlation_id=correlation_id,
        parent_event_id=parent_event_id,
        limit=limit,
    )
    redacted = [redact_event(event.to_dict()) for event in events]

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        path.write_text(
            json.dumps(redacted, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        lines = [json.dumps(event, ensure_ascii=False) for event in redacted]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "output_path": str(path),
        "count": len(redacted),
        "format": fmt,
    }
