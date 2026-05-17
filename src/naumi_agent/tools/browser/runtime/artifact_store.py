"""Session-based artifact management with retention controls.

Ported from browser-debugging-daemon/scripts/runtime/ArtifactStore.js.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _sanitize_segment(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\-_]+", "-", value.strip().lower())[:60]
    return cleaned or "artifact"


class ArtifactStore:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.root_dir = self.base_dir / "artifacts"
        self.current_view_path = self.base_dir / "current_view.png"
        self.session_id: str | None = None
        self.session_dir: Path | None = None
        self.screenshots_dir: Path | None = None
        self.videos_dir: Path | None = None
        self.traces_dir: Path | None = None
        self.events_path: Path | None = None
        self.trace_path: Path | None = None
        self.step_counter: int = 0

        raw_sessions = os.environ.get("BROWSER_ARTIFACT_MAX_SESSIONS", "")
        self.max_sessions: int | None = (
            int(raw_sessions) if raw_sessions.strip().isdigit() else None
        )
        raw_age = os.environ.get("BROWSER_ARTIFACT_MAX_AGE_DAYS", "")
        self.max_age_ms: float | None = (
            float(raw_age) * 24 * 60 * 60 * 1000
            if raw_age.strip()
            and re.match(r"^\d+(\.\d+)?$", raw_age.strip())
            else None
        )

    def start_session(self) -> None:
        self.cleanup_retained_sessions()
        session_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S-%f")
        session_dir = self.root_dir / session_id

        self.session_id = session_id
        self.session_dir = session_dir
        self.screenshots_dir = session_dir / "screenshots"
        self.videos_dir = session_dir / "videos"
        self.traces_dir = session_dir / "traces"
        self.events_path = session_dir / "events.jsonl"
        self.trace_path = None
        self.step_counter = 0

        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        self.traces_dir.mkdir(parents=True, exist_ok=True)

    def cleanup_retained_sessions(self) -> None:
        if not self.root_dir.exists():
            return

        entries: list[dict[str, Any]] = []
        for child in sorted(self.root_dir.iterdir()):
            if child.is_dir():
                try:
                    mtime = child.stat().st_mtime
                except OSError:
                    continue
                entries.append({"path": child, "mtime_ms": mtime * 1000})

        entries.sort(key=lambda e: e["mtime_ms"], reverse=True)
        now_ms = datetime.now().timestamp() * 1000
        kept = 0
        for entry in entries:
            exceeds_age = (
                self.max_age_ms is not None
                and (now_ms - entry["mtime_ms"]) > self.max_age_ms
            )
            exceeds_count = self.max_sessions is not None and kept >= self.max_sessions
            if exceeds_age or exceeds_count:
                import shutil
                shutil.rmtree(entry["path"], ignore_errors=True)
                continue
            kept += 1

    def _ensure_active(self) -> None:
        if self.session_dir is None:
            raise RuntimeError("Artifact session has not been initialized.")

    def get_video_dir(self) -> Path:
        self._ensure_active()
        assert self.videos_dir is not None
        return self.videos_dir

    def get_video_path(self, label: str = "video") -> Path:
        self._ensure_active()
        assert self.videos_dir is not None
        return self.videos_dir / f"{_sanitize_segment(label)}_{_now_ms()}.webm"

    def get_attached_frames_dir(self, label: str = "attached-frames") -> Path:
        self._ensure_active()
        assert self.videos_dir is not None
        frames_dir = self.videos_dir / f"{_sanitize_segment(label)}_{_now_ms()}"
        frames_dir.mkdir(parents=True, exist_ok=True)
        return frames_dir

    def get_current_view_path(self) -> Path:
        return self.current_view_path

    def get_step_screenshot_path(self, label: str) -> Path:
        self._ensure_active()
        assert self.screenshots_dir is not None
        self.step_counter += 1
        filename = f"{self.step_counter:03d}_{_sanitize_segment(label)}.png"
        return self.screenshots_dir / filename

    def get_trace_path(self, label: str = "trace") -> Path:
        self._ensure_active()
        assert self.traces_dir is not None
        self.trace_path = self.traces_dir / f"{_sanitize_segment(label)}_{_now_ms()}.zip"
        return self.trace_path

    def get_console_log_path(self) -> Path:
        self._ensure_active()
        assert self.session_dir is not None
        return self.session_dir / "console.json"

    def get_network_log_path(self) -> Path:
        self._ensure_active()
        assert self.session_dir is not None
        return self.session_dir / "network.json"

    def get_error_log_path(self) -> Path:
        self._ensure_active()
        assert self.session_dir is not None
        return self.session_dir / "errors.json"

    def list_trace_files(self) -> list[Path]:
        if not self.traces_dir or not self.traces_dir.exists():
            return []
        return sorted(self.traces_dir.glob("*.zip"))

    def append_event(self, event_type: str, payload: Any = None) -> None:
        self._ensure_active()
        assert self.events_path is not None
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": event_type,
            "payload": payload or {},
        }
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def write_json(self, filename: str, data: Any) -> Path:
        self._ensure_active()
        assert self.session_dir is not None
        path = self.session_dir / filename
        path.write_text(json.dumps(data, indent=2, default=str), "utf-8")
        return path

    def write_text(self, filename: str, text: str) -> Path:
        self._ensure_active()
        assert self.session_dir is not None
        path = self.session_dir / filename
        path.write_text(text, "utf-8")
        return path

    def list_video_files(self) -> list[Path]:
        if not self.videos_dir or not self.videos_dir.exists():
            return []
        return sorted(self.videos_dir.glob("*.webm"))

    def get_summary(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "sessionDir": str(self.session_dir) if self.session_dir else None,
            "currentViewPath": str(self.current_view_path),
            "screenshotsDir": str(self.screenshots_dir) if self.screenshots_dir else None,
            "videosDir": str(self.videos_dir) if self.videos_dir else None,
            "videoFiles": [str(p) for p in self.list_video_files()],
            "tracesDir": str(self.traces_dir) if self.traces_dir else None,
            "traceFiles": [str(p) for p in self.list_trace_files()],
            "eventsPath": str(self.events_path) if self.events_path else None,
            "tracePath": str(self.trace_path) if self.trace_path else None,
        }


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)
