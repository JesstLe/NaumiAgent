"""Stable JSONL protocol shared by NaumiAgent terminal frontends.

The protocol is intentionally transport-agnostic.  The first implementation
uses stdin/stdout JSONL so an Ink/pi-tui frontend can run as an independent
process while Python remains the single owner of AgentEngine, tools, memory,
safety, and debug tracing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from naumi_agent.ui.messages.base import UIMessage

PROTOCOL_VERSION = 1


class ClientEventType(StrEnum):
    """Events accepted from a terminal frontend."""

    HELLO = "hello"
    SUBMIT = "submit"
    SET_MODE = "set_mode"
    CYCLE_MODE = "cycle_mode"
    PERMISSION_RESPONSE = "permission_response"
    RESUME = "resume"
    PING = "ping"
    SHUTDOWN = "shutdown"


class ServerEventType(StrEnum):
    """Events emitted to a terminal frontend."""

    READY = "ready"
    ACK = "ack"
    ERROR = "error"
    PONG = "pong"
    USER_MESSAGE = "user/message"
    UI_MESSAGE = "ui/message"
    ENGINE_EVENT = "engine/event"
    RUN_STARTED = "run/started"
    RUN_COMPLETED = "run/completed"
    SESSION_REPLAYED = "session/replayed"
    STATUS = "runtime/status"
    MODE_CHANGED = "mode/changed"
    PERMISSION_REQUEST = "permission/request"
    PERMISSION_RESOLVED = "permission/resolved"
    DEBUG_TRACE = "debug/trace"
    SHUTDOWN = "shutdown"


def make_envelope(
    event: ServerEventType | str,
    payload: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
    sequence: int | None = None,
) -> dict[str, Any]:
    """Build one protocol envelope."""
    record: dict[str, Any] = {
        "type": str(event),
        "version": PROTOCOL_VERSION,
        "id": uuid4().hex[:12],
        "ts": datetime.now().isoformat(),
        "payload": payload or {},
    }
    if request_id:
        record["request_id"] = request_id
    if sequence is not None:
        record["seq"] = sequence
    return record


def ui_message_payload(message: UIMessage) -> dict[str, Any]:
    """Serialize a typed UIMessage into a JSON-safe payload."""
    data = asdict(message) if is_dataclass(message) else dict(message)  # type: ignore[arg-type]
    data["type"] = str(data.get("type", ""))
    return data


def encode_jsonl(record: dict[str, Any]) -> str:
    """Serialize a protocol record as strict LF-framed JSONL."""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def decode_jsonl_line(line: str) -> dict[str, Any]:
    """Decode and validate a single client JSONL line."""
    raw = line.rstrip("\r\n")
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSONL 解析失败: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("JSONL 记录必须是对象。")
    if "type" not in value:
        raise ValueError("JSONL 记录缺少 type 字段。")
    return value
