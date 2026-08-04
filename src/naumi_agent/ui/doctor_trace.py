"""Privacy-bounded typed index over one local DebugTrace JSONL run."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DOCTOR_TRACE_SCHEMA_VERSION = 1
DOCTOR_TRACE_DEFAULT_LIMIT = 80
DOCTOR_TRACE_MAX_LIMIT = 200
DOCTOR_TRACE_MAX_QUERY_CHARS = 128
DOCTOR_TRACE_MAX_TAIL_BYTES = 2 * 1024 * 1024
DOCTOR_TRACE_MAX_LINE_BYTES = 64 * 1024

_SAFE_EVENT_RE = re.compile(r"^[a-zA-Z0-9_.:/-]{1,128}$")
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_.:@/-]{1,128}$")
_SAFE_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_SENSITIVE_ATOM_RE = re.compile(
    r"(?:^|[_:/.-])(?:api[_-]?key|authorization|bearer|password|secret|token|sk-)",
    re.IGNORECASE,
)
_IDENTIFIER_KEYS = (
    "run_id",
    "request_id",
    "call_id",
    "task_id",
    "session_id",
    "agent_id",
)
_SAFE_SUMMARY_KEYS = ("phase", "status", "name", "where", "type", "source", "sink")
_BODY_KEYS = {
    "text",
    "content",
    "message",
    "trace",
    "reasoning",
    "arguments",
    "args",
    "prompt",
    "task",
}


class DoctorTraceIndexError(ValueError):
    """Trace index cannot be built without crossing its safety contract."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DoctorTraceEntry:
    cursor: int
    timestamp: str
    event_type: str
    severity: str
    summary: str
    identifiers: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "severity": self.severity,
            "summary": self.summary,
            "identifiers": dict(self.identifiers),
        }


@dataclass(frozen=True)
class DoctorTraceIndex:
    status: str
    diagnostic_code: str
    run_id: str
    interface: str
    assessed_at: str
    query: str
    limit: int
    source_size_bytes: int
    window_size_bytes: int
    window_event_count: int
    matched_event_count: int
    malformed_line_count: int
    truncated: bool
    entries: tuple[DoctorTraceEntry, ...]
    snapshot_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DOCTOR_TRACE_SCHEMA_VERSION,
            "status": self.status,
            "diagnostic_code": self.diagnostic_code,
            "run_id": self.run_id,
            "interface": self.interface,
            "assessed_at": self.assessed_at,
            "query": self.query,
            "limit": self.limit,
            "source_size_bytes": self.source_size_bytes,
            "window_size_bytes": self.window_size_bytes,
            "window_event_count": self.window_event_count,
            "matched_event_count": self.matched_event_count,
            "malformed_line_count": self.malformed_line_count,
            "truncated": self.truncated,
            "entries": [entry.to_dict() for entry in self.entries],
            "snapshot_sha256": self.snapshot_sha256,
            "privacy_notice": (
                "正文、模型输出、reasoning、参数、异常 message 与 traceback 默认折叠，"
                "本索引只包含事件元数据和安全标识符。"
            ),
        }


def build_doctor_trace_index(
    base_dir: Path,
    *,
    query: str = "",
    limit: int = DOCTOR_TRACE_DEFAULT_LIMIT,
    preferred_run_id: str = "",
) -> DoctorTraceIndex:
    """Build a stable bounded index for the preferred or latest local run."""

    normalized_query = _normalize_query(query)
    normalized_limit = _normalize_limit(limit)
    root = Path(base_dir).expanduser().resolve()
    run_dir = _select_run_dir(root, preferred_run_id=preferred_run_id)
    manifest = _read_manifest(run_dir / "manifest.json", run_dir=run_dir)
    run_id = _safe_identifier(manifest.get("run_id")) or run_dir.name
    interface = _safe_identifier(manifest.get("interface")) or "unknown"
    events_path = (run_dir / "events.jsonl").resolve()
    if not events_path.is_relative_to(run_dir.resolve()):
        raise DoctorTraceIndexError(
            "调试事件文件越过运行目录，已拒绝读取。",
            code="trace_path_escape",
        )

    chunk, source_size, start_offset, stable, mtime_ns = _read_stable_tail(events_path)
    parsed, malformed = _parse_tail(chunk, start_offset=start_offset)
    entries = tuple(
        entry
        for cursor, event in parsed
        if (entry := _project_event(cursor, event)) is not None
    )
    filtered = tuple(entry for entry in entries if _matches(entry, normalized_query))
    selected = tuple(reversed(filtered[-normalized_limit:]))
    truncated = start_offset > 0 or len(filtered) > len(selected)
    status = "ready" if stable and malformed == 0 else "degraded"
    diagnostic_code = (
        "trace_index_ready"
        if status == "ready"
        else "trace_source_changing"
        if not stable
        else "trace_malformed_lines"
    )
    assessed_at = datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=UTC).isoformat()
    unsigned = {
        "schema_version": DOCTOR_TRACE_SCHEMA_VERSION,
        "status": status,
        "diagnostic_code": diagnostic_code,
        "run_id": run_id,
        "interface": interface,
        "assessed_at": assessed_at,
        "query": normalized_query,
        "limit": normalized_limit,
        "source_size_bytes": source_size,
        "window_size_bytes": len(chunk),
        "window_event_count": len(entries),
        "matched_event_count": len(filtered),
        "malformed_line_count": malformed,
        "truncated": truncated,
        "entries": [entry.to_dict() for entry in selected],
    }
    digest = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    return DoctorTraceIndex(
        status=status,
        diagnostic_code=diagnostic_code,
        run_id=run_id,
        interface=interface,
        assessed_at=assessed_at,
        query=normalized_query,
        limit=normalized_limit,
        source_size_bytes=source_size,
        window_size_bytes=len(chunk),
        window_event_count=len(entries),
        matched_event_count=len(filtered),
        malformed_line_count=malformed,
        truncated=truncated,
        entries=selected,
        snapshot_sha256=digest,
    )


def render_doctor_trace_index(index: DoctorTraceIndex) -> str:
    """Render the typed index for CLI and Textual fallback surfaces."""

    lines = [
        "## Doctor Trace 索引",
        "",
        f"运行 `{index.run_id}` · {index.interface} · 状态 **{index.status}**",
        (
            f"窗口事件 {index.window_event_count} · 匹配 {index.matched_event_count} · "
            f"显示 {len(index.entries)}/{index.limit} · 损坏行 {index.malformed_line_count}"
        ),
    ]
    if index.query:
        lines.append(f"筛选：`{_markdown_code(index.query)}`")
    if index.truncated:
        lines.append("范围：仅索引最近 2 MiB 或最近 limit 条匹配；更早事件未读取。")
    lines.extend(["", "### 事件", ""])
    if not index.entries:
        lines.append("当前筛选没有匹配事件。")
    for entry in index.entries:
        identifiers = " · ".join(f"{key}={value}" for key, value in entry.identifiers)
        suffix = f" · {identifiers}" if identifiers else ""
        lines.append(
            f"- **{entry.severity}** · `{entry.event_type}` · "
            f"{entry.timestamp or '-'} · {entry.summary}{suffix}"
        )
    lines.extend(
        [
            "",
            f"Snapshot `{index.snapshot_sha256[:16]}` · 诊断码 `{index.diagnostic_code}`",
            "",
            "> 正文、模型输出、reasoning、参数、异常 message 与 traceback 默认折叠。",
        ]
    )
    return "\n".join(lines)


def doctor_trace_payload(index: DoctorTraceIndex) -> dict[str, Any]:
    return index.to_dict()


def _normalize_query(query: str) -> str:
    normalized = " ".join(str(query or "").split())
    if len(normalized) > DOCTOR_TRACE_MAX_QUERY_CHARS:
        raise DoctorTraceIndexError(
            f"Trace 筛选最多 {DOCTOR_TRACE_MAX_QUERY_CHARS} 个字符。",
            code="trace_query_too_long",
        )
    if any(ord(char) < 32 for char in normalized):
        raise DoctorTraceIndexError(
            "Trace 筛选包含不支持的控制字符。",
            code="trace_query_invalid",
        )
    return normalized


def _normalize_limit(limit: int) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError) as exc:
        raise DoctorTraceIndexError(
            "Trace limit 必须是整数。",
            code="trace_limit_invalid",
        ) from exc
    if not 1 <= parsed <= DOCTOR_TRACE_MAX_LIMIT:
        raise DoctorTraceIndexError(
            f"Trace limit 必须在 1..{DOCTOR_TRACE_MAX_LIMIT}。",
            code="trace_limit_invalid",
        )
    return parsed


def _select_run_dir(root: Path, *, preferred_run_id: str) -> Path:
    if not root.is_dir():
        raise DoctorTraceIndexError(
            "当前没有可读取的 debug-runs 目录。",
            code="trace_root_missing",
        )
    if preferred_run_id:
        safe_run_id = str(preferred_run_id)
        if not _SAFE_RUN_ID_RE.fullmatch(safe_run_id) or ".." in safe_run_id:
            raise DoctorTraceIndexError(
                "Trace run_id 格式无效。",
                code="trace_run_id_invalid",
            )
        run_dir = (root / safe_run_id).resolve()
    else:
        candidates: list[tuple[int, Path]] = []
        try:
            children = tuple(root.iterdir())
        except OSError as exc:
            raise DoctorTraceIndexError(
                "当前 debug-runs 目录不可读。",
                code="trace_root_unreadable",
            ) from exc
        for child in children:
            events_path = child / "events.jsonl"
            try:
                if child.is_dir() and events_path.is_file():
                    candidates.append((events_path.stat().st_mtime_ns, child.resolve()))
            except OSError:
                continue
        if not candidates:
            raise DoctorTraceIndexError(
                "当前没有可索引的调试运行。",
                code="trace_run_missing",
            )
        run_dir = max(candidates, key=lambda item: item[0])[1]
    if not run_dir.is_relative_to(root) or not (run_dir / "events.jsonl").is_file():
        raise DoctorTraceIndexError(
            "指定调试运行不存在或越过状态目录。",
            code="trace_run_missing",
        )
    return run_dir


def _read_manifest(path: Path, *, run_dir: Path) -> dict[str, Any]:
    try:
        if not path.resolve().is_relative_to(run_dir.resolve()):
            return {}
        if path.stat().st_size > 64 * 1024:
            return {}
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_stable_tail(path: Path) -> tuple[bytes, int, int, bool, int]:
    last: tuple[bytes, int, int, bool, int] | None = None
    for _attempt in range(2):
        try:
            before = path.stat()
            start = max(0, before.st_size - DOCTOR_TRACE_MAX_TAIL_BYTES)
            with path.open("rb") as handle:
                handle.seek(start)
                chunk = handle.read(DOCTOR_TRACE_MAX_TAIL_BYTES)
            after = path.stat()
        except OSError as exc:
            raise DoctorTraceIndexError(
                "调试事件文件暂时不可读。",
                code="trace_read_failed",
            ) from exc
        stable = before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
        last = (chunk, after.st_size, start, stable, after.st_mtime_ns)
        if stable:
            return last
    assert last is not None
    return last


def _parse_tail(chunk: bytes, *, start_offset: int) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    if start_offset > 0:
        newline = chunk.find(b"\n")
        if newline < 0:
            return [], 1
        start_offset += newline + 1
        chunk = chunk[newline + 1 :]
    parsed: list[tuple[int, dict[str, Any]]] = []
    malformed = 0
    cursor = start_offset
    for raw_line in chunk.splitlines(keepends=True):
        line = raw_line.rstrip(b"\r\n")
        line_cursor = cursor
        cursor += len(raw_line)
        if not line:
            continue
        if len(line) > DOCTOR_TRACE_MAX_LINE_BYTES:
            malformed += 1
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed += 1
            continue
        if not isinstance(value, dict):
            malformed += 1
            continue
        parsed.append((line_cursor, value))
    return parsed, malformed


def _project_event(cursor: int, event: dict[str, Any]) -> DoctorTraceEntry | None:
    event_type = _safe_atom(event.get("event")) or "invalid_event_name"
    timestamp = _safe_timestamp(event.get("ts"))
    data = event.get("data")
    if not isinstance(data, dict):
        data = {}
    identifiers = _collect_identifiers(event, data)
    severity = _severity(event_type, data)
    summary = _summary(event_type, data)
    return DoctorTraceEntry(
        cursor=max(0, cursor),
        timestamp=timestamp,
        event_type=event_type,
        severity=severity,
        summary=summary,
        identifiers=identifiers,
    )


def _collect_identifiers(
    event: dict[str, Any],
    data: dict[str, Any],
) -> tuple[tuple[str, str], ...]:
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    result: list[tuple[str, str]] = []
    for key in _IDENTIFIER_KEYS:
        for source in (event, data, nested):
            value = _safe_identifier(source.get(key))
            if value:
                result.append((key, value))
                break
    return tuple(result)


def _severity(event_type: str, data: dict[str, Any]) -> str:
    lowered = event_type.lower()
    stream_type = str(data.get("event") or "").lower()
    status = str(data.get("status") or "").lower()
    if "exception" in lowered or "error" in lowered or stream_type == "error" or status in {
        "error",
        "failed",
        "failure",
    }:
        return "error"
    if any(token in lowered for token in ("warning", "degraded", "stale")) or status in {
        "warning",
        "degraded",
        "stale",
    }:
        return "warning"
    return "info"


def _summary(event_type: str, data: dict[str, Any]) -> str:
    if event_type == "input":
        return "用户输入（正文已折叠）"
    if event_type == "output":
        return "界面输出（正文已折叠）"
    if event_type == "exception":
        safe_type = _safe_atom(data.get("type"))
        safe_where = _safe_atom(data.get("where"))
        details = " · ".join(value for value in (safe_type, safe_where) if value)
        return f"异常元数据{f' · {details}' if details else ''}（正文已折叠）"
    if event_type == "engine.stream_event":
        stream_type = _safe_atom(data.get("event")) or "unknown"
        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        tool = _safe_atom(nested.get("name"))
        return f"运行事件 {stream_type}{f' · {tool}' if tool else ''}（正文已折叠）"
    details: list[str] = []
    for key in _SAFE_SUMMARY_KEYS:
        if key in _BODY_KEYS:
            continue
        value = _safe_atom(data.get(key))
        if value:
            details.append(f"{key}={value}")
    return " · ".join(details[:3]) or "事件元数据（正文已折叠）"


def _matches(entry: DoctorTraceEntry, query: str) -> bool:
    if not query:
        return True
    needle = query.casefold()
    haystack = " ".join(
        (
            entry.event_type,
            entry.severity,
            entry.summary,
            *(f"{key}={value}" for key, value in entry.identifiers),
        )
    ).casefold()
    return needle in haystack


def _safe_identifier(value: Any) -> str:
    text = str(value or "")
    if _SENSITIVE_ATOM_RE.search(text):
        return ""
    return text if _SAFE_ID_RE.fullmatch(text) else ""


def _safe_atom(value: Any) -> str:
    text = str(value or "")
    if _SENSITIVE_ATOM_RE.search(text):
        return ""
    return text if _SAFE_EVENT_RE.fullmatch(text) else ""


def _safe_timestamp(value: Any) -> str:
    text = str(value or "")
    if not text or len(text) > 64:
        return ""
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return text


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _markdown_code(value: str) -> str:
    normalized = value.replace("`", "'")
    return normalized[:DOCTOR_TRACE_MAX_QUERY_CHARS]


__all__ = [
    "DOCTOR_TRACE_DEFAULT_LIMIT",
    "DOCTOR_TRACE_MAX_LIMIT",
    "DoctorTraceEntry",
    "DoctorTraceIndex",
    "DoctorTraceIndexError",
    "build_doctor_trace_index",
    "doctor_trace_payload",
    "render_doctor_trace_index",
]
