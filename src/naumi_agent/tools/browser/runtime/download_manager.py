"""File download management for browser sessions.

Ported from browser-debugging-daemon/scripts/runtime/DownloadManager.js.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_TRACKED = 100


class DownloadManager:
    def __init__(self, artifacts_dir: str | Path) -> None:
        self._downloads_dir = Path(artifacts_dir) / "downloads"
        self._downloads_dir.mkdir(parents=True, exist_ok=True)
        self._downloads: list[dict[str, Any]] = []
        self._max_tracked = DEFAULT_MAX_TRACKED
        self._page: Any = None

    def attach(self, page: Any) -> None:
        if page is None:
            return
        self.detach()
        self._page = page
        page.on("download", self._handle_download)

    def detach(self) -> None:
        self._page = None

    async def _handle_download(self, download: Any) -> None:
        dl_id = str(uuid.uuid4())
        suggested = download.suggested_filename or "download"
        url = download.url
        safe_name = _safe_filename(suggested)
        dest_dir = self._downloads_dir / dl_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / safe_name

        entry: dict[str, Any] = {
            "id": dl_id,
            "filename": safe_name,
            "url": url,
            "suggestedFilename": suggested,
            "status": "downloading",
            "startedAt": datetime.now().isoformat(),
            "finishedAt": None,
            "size": None,
            "error": None,
            "path": str(dest_path),
        }
        self._downloads.append(entry)
        while len(self._downloads) > self._max_tracked:
            self._downloads.pop(0)

        try:
            await download.save_as(str(dest_path))
            stat = dest_path.stat()
            entry["status"] = "completed"
            entry["size"] = stat.st_size
        except Exception as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
        entry["finishedAt"] = datetime.now().isoformat()

    def list_downloads(
        self, *, limit: int = 50, status: str | None = None
    ) -> list[dict[str, Any]]:
        result = self._downloads
        if status:
            result = [d for d in result if d.get("status") == status]
        return [
            {
                "id": d["id"],
                "filename": d["filename"],
                "url": d["url"],
                "status": d["status"],
                "size": d["size"],
                "startedAt": d["startedAt"],
                "finishedAt": d["finishedAt"],
                "error": d["error"],
            }
            for d in result[-limit:]
        ]

    def get(self, dl_id: str) -> dict[str, Any] | None:
        for d in self._downloads:
            if d["id"] == dl_id:
                return {**d, "exists": Path(d["path"]).exists()}
        return None

    def clear(self) -> None:
        self._downloads.clear()


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._\-]", "_", name)[:200]
    return cleaned or "download"
