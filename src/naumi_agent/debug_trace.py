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
        return (
            f"调试日志目录: {self.run_dir}\n"
            f"- 结构化事件: {self.events_path}\n"
            f"- 可读输出: {self.transcript_path}\n"
            f"- 运行元数据: {self.manifest_path}"
        )

    def close(self) -> None:
        if self._closed:
            return
        self.event("trace_closed", {})
        self._closed = True

