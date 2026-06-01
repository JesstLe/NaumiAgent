"""Structured debug trace for CLI/TUI runs."""

from __future__ import annotations

import json
import os
import platform
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return repr(value)


def resolve_events_path(path: Path) -> Path:
    """Resolve a debug run directory or events file to events.jsonl."""
    candidate = path.expanduser()
    if candidate.is_dir():
        candidate = candidate / "events.jsonl"
    return candidate.resolve()


def find_latest_run(base_dir: Path) -> Path | None:
    """Return the newest debug run directory under base_dir."""
    root = base_dir.expanduser()
    if not root.exists():
        return None
    runs = [
        path for path in root.iterdir()
        if path.is_dir() and (path / "events.jsonl").exists()
    ]
    if not runs:
        return None
    return max(runs, key=lambda path: (path / "events.jsonl").stat().st_mtime)


def render_debug_replay(path: Path, *, max_events: int = 500) -> str:
    """Render a structured debug trace as a readable event replay."""
    events_path = resolve_events_path(path)
    if not events_path.exists():
        return f"未找到调试事件文件: {events_path}"

    events: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)

    shown = events[-max_events:] if max_events > 0 else events
    lines = [
        "NaumiAgent Debug Replay",
        f"events: {events_path}",
        f"shown: {len(shown)}/{len(events)}",
        "",
    ]
    for event in shown:
        lines.extend(DebugTrace._format_event(event))
    return "\n".join(lines).rstrip() + "\n"


class DebugTrace:
    """Append-only JSONL trace plus plain transcript for one UI run."""

    def __init__(
        self,
        *,
        run_id: str,
        run_dir: Path,
        interface: str,
        enabled: bool = True,
    ) -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        self.interface = interface
        self.enabled = enabled
        self.events_path = self.run_dir / "events.jsonl"
        self.transcript_path = self.run_dir / "transcript.txt"
        self.manifest_path = self.run_dir / "manifest.json"
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def create(
        cls,
        *,
        interface: str,
        base_dir: Path,
        metadata: dict[str, Any] | None = None,
    ) -> DebugTrace:
        enabled = os.environ.get("NAUMI_DEBUG_TRACE", "1").lower() not in _FALSE_VALUES
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        run_dir = base_dir.expanduser().resolve() / run_id
        trace = cls(run_id=run_id, run_dir=run_dir, interface=interface, enabled=enabled)
        if enabled:
            run_dir.mkdir(parents=True, exist_ok=True)
            trace._write_manifest(metadata or {})
            trace.event("trace_started", {"metadata": metadata or {}})
        return trace

    def _write_manifest(self, metadata: dict[str, Any]) -> None:
        manifest = {
            "run_id": self.run_id,
            "interface": self.interface,
            "started_at": datetime.now().isoformat(),
            "cwd": str(Path.cwd()),
            "pid": os.getpid(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "events_path": str(self.events_path),
            "transcript_path": str(self.transcript_path),
            "metadata": _json_safe(metadata),
        }
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def event(self, name: str, data: dict[str, Any] | None = None) -> None:
        if not self.enabled or self._closed:
            return
        entry = {
            "ts": datetime.now().isoformat(),
            "monotonic": time.monotonic(),
            "run_id": self.run_id,
            "interface": self.interface,
            "event": name,
            "data": _json_safe(data or {}),
        }
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(line)

    def input(self, source: str, text: str, **extra: Any) -> None:
        self.event("input", {"source": source, "text": text, **extra})

    def output(self, sink: str, text: str, **extra: Any) -> None:
        self.event("output", {"sink": sink, "text": text, **extra})
        if self.enabled and not self._closed:
            with self._lock:
                with self.transcript_path.open("a", encoding="utf-8") as f:
                    f.write(text)

    def exception(self, where: str, exc: BaseException, **extra: Any) -> None:
        self.event(
            "exception",
            {
                "where": where,
                "type": type(exc).__name__,
                "message": str(exc),
                **extra,
            },
        )

    def describe(self) -> str:
        if not self.enabled:
            return "结构化调试日志已通过 NAUMI_DEBUG_TRACE=0 禁用。"
        lines = [
            f"调试日志目录: {self.run_dir}",
            f"- 结构化事件: {self.events_path}",
            f"- 可读输出: {self.transcript_path}",
            f"- 运行元数据: {self.manifest_path}",
        ]
        metadata = self._read_manifest_metadata()
        if metadata:
            lines.extend([
                "",
                "运行路径:",
                f"- 启动目录: {metadata.get('cwd', '-')}",
                f"- 配置文件: {metadata.get('config_path', '-')}",
                f"- 工作区: {metadata.get('workspace_root', '-')}",
                f"- 会话库: {metadata.get('session_db_path', '-')}",
                f"- 向量库: {metadata.get('vector_db_path', '-')}",
                f"- debug-runs: {metadata.get('debug_runs_dir', '-')}",
            ])
        return "\n".join(lines)

    def _read_manifest_metadata(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {}
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(manifest, dict):
            return {}
        metadata = manifest.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        cwd = manifest.get("cwd")
        if isinstance(cwd, str):
            return {"cwd": cwd, **metadata}
        return metadata

    def build_diagnostic_text(self, scope: str = "last") -> str:
        """Build a copy-friendly diagnostic excerpt from structured events."""
        if not self.enabled:
            return "结构化调试日志已禁用，无法生成诊断片段。"
        events = self._read_events()
        if not events:
            return "暂无结构化调试事件。"

        normalized_scope = scope.strip().lower()
        if normalized_scope in {"all", "full", "transcript"}:
            if self.transcript_path.exists():
                return self.transcript_path.read_text(encoding="utf-8")
            return "暂无可读输出记录。"
        if normalized_scope == "error":
            return self._build_error_diagnostic(events)
        return self._build_last_turn_diagnostic(events)

    def _read_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _build_last_turn_diagnostic(self, events: list[dict[str, Any]]) -> str:
        start = self._find_last_event_index(events, "input")
        selected = events[start:] if start is not None else events
        return self._format_events("最近一轮诊断记录", selected)

    def _build_error_diagnostic(self, events: list[dict[str, Any]]) -> str:
        error_index = self._find_last_error_index(events)
        if error_index is None:
            return self._build_last_turn_diagnostic(events)
        input_index = self._find_last_event_index(events, "input", before=error_index)
        start = input_index if input_index is not None else max(0, error_index - 12)
        end = min(len(events), error_index + 8)
        return self._format_events("最近错误诊断记录", events[start:end])

    @staticmethod
    def _find_last_event_index(
        events: list[dict[str, Any]],
        name: str,
        *,
        before: int | None = None,
    ) -> int | None:
        stop = len(events) if before is None else before + 1
        for index in range(stop - 1, -1, -1):
            if events[index].get("event") == name:
                return index
        return None

    @staticmethod
    def _find_last_error_index(events: list[dict[str, Any]]) -> int | None:
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            name = event.get("event")
            data = event.get("data", {})
            if name == "exception":
                return index
            if name == "engine.stream_event" and isinstance(data, dict):
                if data.get("event") == "error":
                    return index
            if name in {"cli.submit_error", "tui.agent_run_error"}:
                return index
        return None

    def _format_events(self, title: str, events: list[dict[str, Any]]) -> str:
        lines = [
            title,
            f"run_id: {self.run_id}",
            f"interface: {self.interface}",
            f"events: {self.events_path}",
            "",
        ]
        for event in events:
            lines.extend(self._format_event(event))
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _format_event(event: dict[str, Any]) -> list[str]:
        name = str(event.get("event", "event"))
        ts = str(event.get("ts", ""))
        data = event.get("data", {})
        if not isinstance(data, dict):
            data = {"value": data}

        if name == "input":
            source = data.get("source", "input")
            return [f"[{ts}] INPUT {source}", str(data.get("text", "")), ""]
        if name == "output":
            sink = data.get("sink", "output")
            text = str(data.get("text", ""))
            return [f"[{ts}] OUTPUT {sink}", text, ""]
        if name == "engine.stream_event":
            stream_event = data.get("event", "")
            stream_data = data.get("data", {})
            if isinstance(stream_data, dict) and "content" in stream_data:
                content = str(stream_data.get("content", ""))
                return [f"[{ts}] STREAM {stream_event}", content, ""]
            return [
                f"[{ts}] STREAM {stream_event}",
                json.dumps(_json_safe(stream_data), ensure_ascii=False, indent=2),
                "",
            ]
        if name == "exception":
            return [
                f"[{ts}] EXCEPTION {data.get('where', '')}",
                str(data.get("trace") or data.get("message") or ""),
                "",
            ]
        return [
            f"[{ts}] EVENT {name}",
            json.dumps(_json_safe(data), ensure_ascii=False, indent=2),
            "",
        ]

    def close(self) -> None:
        if self._closed:
            return
        self.event("trace_closed", {})
        self._closed = True
