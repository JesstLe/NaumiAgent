"""JSON file persistence for task runs.

Ported from browser-debugging-daemon/scripts/orchestrator/TaskRunStore.js (93 lines).
Each run is stored as a separate JSON file; index.json holds summaries.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TaskRunStore:
    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)
        self._runs_dir = self._base_dir / "task-runs"
        self._index_path = self._runs_dir / "index.json"
        self._runs_dir.mkdir(parents=True, exist_ok=True)

    def _run_path(self, run_id: str) -> Path:
        return self._runs_dir / f"{run_id}.json"

    def persist(self, runs: list[dict[str, Any]]) -> None:
        index_entries: list[dict[str, Any]] = []
        for run in runs:
            run_id = run.get("id")
            if not run_id:
                continue

            run_copy = {k: v for k, v in run.items() if k != "promise"}

            path = self._run_path(run_id)
            path.write_text(
                json.dumps(run_copy, indent=2, default=str),
                encoding="utf-8",
            )
            index_entries.append({
                "id": run_id,
                "status": run.get("status"),
                "taskInstruction": (run.get("taskInstruction") or "")[:100],
                "createdAt": run.get("createdAt"),
                "finishedAt": run.get("finishedAt"),
                "summary": (run.get("summary") or "")[:100],
            })

        self._index_path.write_text(
            json.dumps(index_entries, indent=2, default=str),
            encoding="utf-8",
        )

    def load(self) -> list[dict[str, Any]]:
        if not self._runs_dir.exists():
            return []

        runs: list[dict[str, Any]] = []

        try:
            files = [
                f
                for f in self._runs_dir.iterdir()
                if f.suffix == ".json" and f.name != "index.json"
            ]
            for fp in files:
                try:
                    parsed = json.loads(fp.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict) and parsed.get("id"):
                        runs.append(parsed)
                except (json.JSONDecodeError, ValueError, OSError):
                    continue
        except OSError:
            pass

        if runs:
            return runs

        if not self._index_path.exists():
            return []

        try:
            parsed = json.loads(
                self._index_path.read_text(encoding="utf-8")
            )
            if not isinstance(parsed, list):
                return []

            if parsed and "result" in parsed[0]:
                self.persist(parsed)
                return parsed

            return []
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.error("Failed to load task runs: %s", exc)
            return []

    def delete_run(self, run_id: str) -> None:
        path = self._run_path(run_id)
        if path.exists():
            path.unlink()
