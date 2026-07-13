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
    SET_REASONING = "set_reasoning"
    PERMISSION_RESPONSE = "permission_response"
    RESUME = "resume"
    TASK_PANEL = "task_panel"
    TASK_CANCEL = "task_cancel"
    PERMISSIONS_PANEL = "permissions_panel"
    DOCTOR = "doctor"
    PING = "ping"
    SHUTDOWN = "shutdown"


_CLIENT_EVENT_NAMES = {str(event) for event in ClientEventType}
_PAYLOAD_LIMIT_DEFAULTS = {
    ClientEventType.TASK_PANEL: 12,
    ClientEventType.PERMISSIONS_PANEL: 12,
}


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
    WORKBENCH_SNAPSHOT = "workbench/snapshot"
    WORKBENCH_EVENT = "workbench/event"
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


def normalize_client_record(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize and validate one client event before bridge dispatch."""
    if not isinstance(record, dict):
        raise ValueError("客户端事件必须是对象。")

    event_type = str(record.get("type") or "")
    if not event_type:
        raise ValueError("客户端事件缺少 type 字段。")
    if event_type not in _CLIENT_EVENT_NAMES:
        raise ValueError(f"未知客户端事件: {event_type}")

    version = record.get("version")
    if version is not None:
        try:
            parsed_version = int(version)
        except (TypeError, ValueError) as exc:
            raise ValueError("协议 version 必须是整数。") from exc
        if parsed_version != PROTOCOL_VERSION:
            raise ValueError(
                f"协议 version 不兼容: {parsed_version}，当前支持 {PROTOCOL_VERSION}。"
            )

    payload = record.get("payload", {})
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象。")

    normalized = dict(record)
    normalized["type"] = event_type
    normalized["version"] = PROTOCOL_VERSION
    if "id" in normalized and normalized["id"] is not None:
        normalized["id"] = str(normalized["id"])
    if "request_id" in normalized and normalized["request_id"] is not None:
        normalized["request_id"] = str(normalized["request_id"])
    normalized["payload"] = _normalize_client_payload(ClientEventType(event_type), payload)
    return normalized


def _normalize_client_payload(
    event_type: ClientEventType,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if event_type == ClientEventType.SUBMIT:
        return {"text": str(payload.get("text") or "")}

    if event_type == ClientEventType.SET_MODE:
        return {"mode": str(payload.get("mode") or "").strip().lower()}

    if event_type == ClientEventType.SET_REASONING:
        return {"enabled": _to_bool(payload.get("enabled"))}

    if event_type == ClientEventType.PERMISSION_RESPONSE:
        choice = str(payload.get("choice") or "").strip().lower()
        if choice not in {"allow", "deny", "bypass"}:
            raise ValueError("权限选择无效，可用值: allow / deny / bypass。")
        return {
            "request_id": str(payload.get("request_id") or ""),
            "choice": choice,
        }

    if event_type == ClientEventType.RESUME:
        normalized: dict[str, Any] = {
            "session_id": str(payload.get("session_id") or "").strip(),
        }
        if "clear" in payload:
            normalized["clear"] = _to_bool(payload.get("clear"))
        return normalized

    if event_type == ClientEventType.TASK_PANEL:
        detail_id = str(
            payload.get("detail_id") or payload.get("detail") or ""
        ).strip()
        normalized = {
            "limit": _bounded_int(
                payload.get("limit"),
                _PAYLOAD_LIMIT_DEFAULTS[event_type],
                lower=1,
                upper=50,
            ),
            "source": str(payload.get("source") or "all").strip().lower().replace("-", "_"),
            "status": str(payload.get("status") or "all").strip().lower().replace("-", "_"),
            "pinned": _to_bool(payload.get("pinned")),
            "refresh": _to_bool(payload.get("refresh")),
            "history": _to_bool(payload.get("history")),
        }
        if detail_id:
            normalized["detail_id"] = detail_id
        return normalized

    if event_type == ClientEventType.TASK_CANCEL:
        task_id = str(
            payload.get("task_id") or payload.get("id") or payload.get("run_id") or ""
        ).strip()
        source = str(payload.get("source") or "all").strip().lower().replace("-", "_")
        reason = str(payload.get("reason") or "用户从任务面板取消。").strip()
        if not task_id:
            raise ValueError("任务取消缺少 task_id。")
        return {
            "task_id": task_id,
            "source": source or "all",
            "reason": reason or "用户从任务面板取消。",
        }

    if event_type == ClientEventType.PERMISSIONS_PANEL:
        return {
            "limit": _bounded_int(
                payload.get("limit"),
                _PAYLOAD_LIMIT_DEFAULTS[event_type],
                lower=1,
                upper=50,
            ),
        }

    return dict(payload)


def _bounded_int(raw: Any, default: int, *, lower: int, upper: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(value, upper))


def _to_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False
