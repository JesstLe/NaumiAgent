"""Template persistence for browser task runs.

Ported from browser-debugging-daemon/scripts/orchestrator/RunTemplateStore.js (30 lines).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class RunTemplateStore:
    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)
        self._templates_dir = self._base_dir / "task-runs"
        self._templates_path = self._templates_dir / "templates.json"
        self._templates_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[dict[str, Any]]:
        if not self._templates_path.exists():
            return []

        try:
            parsed = json.loads(
                self._templates_path.read_text(encoding="utf-8")
            )
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.error("Failed to load run templates: %s", exc)
            return []

    def persist(self, templates: list[dict[str, Any]]) -> None:
        self._templates_path.write_text(
            json.dumps(templates or [], indent=2, default=str),
            encoding="utf-8",
        )
